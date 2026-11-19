"""Posts envelopes to Emet and keeps the ones that could not be delivered for a later retry."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import VERSION, pause, pending, storage
from .config import Config
from .envelope import REPOSITORY_ATTRIBUTE

TIMEOUT_S = 15
KEY_CHECK_TIMEOUT_S = 5
# No trace has this id, so a key Emet accepts reads Emet's own "not found" for it
PLACEHOLDER_TRACE = "00000000-0000-4000-8000-000000000000"
# Bounds the queue so an unreachable endpoint cannot fill the disk
QUEUE_MAX_FILES = 500
QUEUE_MAX_BYTES = 256 * 1024 * 1024
RETRYABLE = (408, 425, 429)
# Emet's 202 lists the tool spans that carried no content, up to two entries per tool span of a batch
MAX_REPLY_BYTES = 128 * 1024
# A repository folder name as /emet:status shows it
REPOSITORY_MAX = 100


@dataclass(frozen=True)
class Accepted:
    """An envelope Emet accepted, with what /emet:status shows about it."""

    session_id: str
    repository: str
    outcome: Outcome


@dataclass(frozen=True)
class Outcome:
    status: int
    delivered: bool
    final: bool
    detail: str
    # Emet's public id of the conversation an accepted batch landed on, not the session id
    conversation_uuid: str = ""
    # Emet's public id of the trace, which its report is read by
    trace_uuid: str = ""
    # Seconds Emet asked to wait in Retry-After, None when it named none
    retry_after: Optional[float] = None


def post(config: Config, envelope: Dict[str, Any]) -> Outcome:
    """Sends one envelope. final is True when resending would not change the answer."""
    request = urllib.request.Request(
        f"{config.endpoint}/api/v1/traces/",
        data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.emet_key}",
            "Content-Type": "application/json",
            "User-Agent": f"emet-claude-code/{VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            # Delivered once Emet answered; the body only adds the report link
            reply = b""
            with contextlib.suppress(OSError, http.client.HTTPException):
                reply = response.read(MAX_REPLY_BYTES)
            return Outcome(response.status, True, True, "", accepted_conversation(reply), accepted_trace(reply))
    except urllib.error.HTTPError as error:
        body = b""
        # The status is the answer; a body that cannot be read changes nothing
        with contextlib.suppress(OSError, http.client.HTTPException):
            body = error.read(500)
        final = error.code < 500 and error.code not in RETRYABLE
        detail = body.decode("utf-8", "replace")
        return Outcome(error.code, False, final, detail, retry_after=pause.named(error.headers.get("Retry-After")))
    # HTTPException: an answer that is not HTTP, as a broken proxy gives
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        return Outcome(0, False, False, type(error).__name__)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so the key is never sent on to another address."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def miss_code(body: bytes) -> str:
    """Returns the code of Emet's JSON error body, or "" for any other body."""
    try:
        error = json.loads(body)
    except (ValueError, RecursionError):
        return ""
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else ""


def check_key(config: Config) -> str:
    """Asks Emet whether the key is accepted, by reading the report of a trace that does not exist. Tried once.

    Only Emet's own "not found" counts as accepted: a server without the route also answers 404.
    """
    request = urllib.request.Request(
        f"{config.endpoint}/api/v1/traces/{PLACEHOLDER_TRACE}/report/",
        headers={
            "Authorization": f"Bearer {config.emet_key}",
            "Accept": "application/json",
            "User-Agent": f"emet-claude-code/{VERSION}",
        },
        method="GET",
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=KEY_CHECK_TIMEOUT_S) as response:
            return f"unexpected {response.status}"
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return "rejected"
        body = b""
        with contextlib.suppress(OSError, http.client.HTTPException):
            body = error.read(MAX_REPLY_BYTES)
        if error.code == 404 and miss_code(body) == "not_found":
            return "accepted"
        return f"unexpected {error.code}"
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return "unreachable"


def accepted_uuid(body: bytes, field: str) -> str:
    """Returns the uuid Emet's 202 body gives under field, or "" when it has none."""
    try:
        accepted = json.loads(body)
        return str(uuid.UUID(str(accepted[field])))
    except (ValueError, TypeError, KeyError, RecursionError):
        return ""


def accepted_conversation(body: bytes) -> str:
    return accepted_uuid(body, "conversation")


def accepted_trace(body: bytes) -> str:
    return accepted_uuid(body, "trace")


def queued(root: Path) -> List[Path]:
    return sorted((root / "queue").glob("*.json"))


def queue_name(root: Path, envelope: Dict[str, Any]) -> str:
    """Names a queued envelope so that names sort in the order they were queued. The number only
    ever grows, since clocks can repeat a value (Windows) or step back."""
    first = envelope["spans"][0]["span_id"] if envelope.get("spans") else ""
    key = f"{envelope.get('trace_id')}/{first}".encode()
    sequence = time.time_ns()
    files = queued(root)
    if files:
        with contextlib.suppress(ValueError):
            sequence = max(sequence, int(files[-1].name.split("-", 1)[0]) + 1)
    return f"{sequence:020d}-{hashlib.sha256(key).hexdigest()[:16]}.json"


def remove(path: Path) -> None:
    # Another worker may have removed it first
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def enqueue(root: Path, envelope: Dict[str, Any]) -> None:
    with storage.locked(root / "queue.lock"):
        storage.write_json(root / "queue" / queue_name(root, envelope), envelope)
        files = queued(root)
        sizes = {}
        for path in files:
            with contextlib.suppress(FileNotFoundError):
                sizes[path] = path.stat().st_size
        total = sum(sizes.values())
        while files and (len(files) > QUEUE_MAX_FILES or total > QUEUE_MAX_BYTES):
            oldest = files.pop(0)
            total -= sizes.get(oldest, 0)
            remove(oldest)
            storage.log(root, f"queue full, dropped {oldest.name}")


def turn_of(envelope: Dict[str, Any]) -> Tuple[str, str]:
    """Returns the session and the trace id an envelope was built for."""
    return str(envelope.get("conversation_id") or ""), str(envelope.get("trace_id") or "")


def describe(outcome: Outcome) -> str:
    if outcome.delivered:
        return str(outcome.status)
    if outcome.status in (401, 403):
        return f"{outcome.status} key rejected"
    if outcome.status == 0:
        return f"network {outcome.detail}"
    return f"{outcome.status} {outcome.detail[:200]}"


class Sender:
    """Sends envelopes in order. After a failure that may pass, everything after it waits in the queue,
    so batches stay in order and one unreachable endpoint costs one timeout, not one per turn.

    The failure also starts a pause every session shares. While it lasts nothing is requested.
    """

    def __init__(self, root: Path, config: Config) -> None:
        self.root = root
        self.config = config
        self.blocked = False
        # Every envelope Emet accepted in this run, queued ones included
        self.accepted: List[Accepted] = []
        self.failures: List[Outcome] = []
        # Per session, the traces Emet refused for good. Read from the session's own file, then added to by this run
        self.refusals: Dict[str, Set[str]] = {}

    def refused(self, session_id: str) -> Set[str]:
        """Returns the session's traces Emet refused for good, to any worker. Nothing more is sent to them."""
        if session_id not in self.refusals:
            self.refusals[session_id] = pending.refused(self.root, session_id)
        return self.refusals[session_id]

    def is_refused(self, envelope: Dict[str, Any]) -> bool:
        session_id, trace_id = turn_of(envelope)
        return trace_id in self.refused(session_id)

    def held(self) -> bool:
        """Says whether requests wait, checking the shared pause until this run is blocked."""
        if not self.blocked and pause.until(self.root):
            self.blocked = True
            storage.log(self.root, "paused, queued without sending")
        return self.blocked

    def record(self, envelope: Dict[str, Any], outcome: Outcome) -> None:
        if outcome.delivered:
            resource = envelope.get("resource")
            repository = str(resource.get(REPOSITORY_ATTRIBUTE) or "")[:REPOSITORY_MAX] if isinstance(resource, dict) else ""
            session_id = str(envelope.get("conversation_id") or "")
            self.accepted.append(Accepted(session_id, repository, outcome))
            # Kept at once and for the envelope's own session: a queued turn can be delivered by another session's worker
            if envelope.get("end") is True and outcome.trace_uuid:
                pending.note(self.root, session_id, outcome.trace_uuid, str(getattr(self.config, "endpoint", "")))
        else:
            self.failures.append(outcome)
        if outcome.delivered or outcome.final:
            # Emet answered, so it can be reached
            pause.clear(self.root)
            if not outcome.delivered:
                session_id, trace_id = turn_of(envelope)
                self.refused(session_id).add(trace_id)
                # Kept for the envelope's own session, which may not be the one this worker runs for
                pending.refuse(self.root, session_id, trace_id, outcome.status)
        else:
            pause.extend(self.root, outcome.retry_after)

    def send(self, envelopes: List[Dict[str, Any]]) -> None:
        for envelope in envelopes:
            if self.is_refused(envelope):
                storage.log(self.root, f"trace {envelope['trace_id']} was refused, batch not sent")
                continue
            if self.held():
                enqueue(self.root, envelope)
                continue
            outcome = post(self.config, envelope)
            self.record(envelope, outcome)
            storage.log(
                self.root, f"trace {envelope['trace_id']} spans={len(envelope['spans'])} end={envelope['end']} -> {describe(outcome)}"
            )
            if not outcome.delivered and not outcome.final:
                enqueue(self.root, envelope)
                self.blocked = True

    def flush(self) -> int:
        """Retries queued envelopes, oldest first. Returns how many are still queued.

        The queue is shared by every session, so one worker at a time flushes it.
        """
        with storage.locked(self.root / "queue.lock"):
            files = queued(self.root)
            for index, path in enumerate(files):
                # Read under the lock, so of the workers waiting on a pause only the first tries
                if self.held():
                    return len(files) - index
                envelope = storage.read_json(path, None)
                if not isinstance(envelope, dict):
                    remove(path)
                    continue
                if self.is_refused(envelope):
                    storage.log(self.root, f"trace {envelope.get('trace_id')} was refused, queued batch dropped")
                    remove(path)
                    continue
                outcome = post(self.config, envelope)
                self.record(envelope, outcome)
                storage.log(self.root, f"retry {envelope.get('trace_id')} -> {describe(outcome)}")
                if not outcome.delivered and not outcome.final:
                    self.blocked = True
                    return len(files) - index
                remove(path)
            return 0
