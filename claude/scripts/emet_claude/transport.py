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
from typing import Any, Dict, List

from . import VERSION, storage
from .config import Config
from .envelope import REPOSITORY_ATTRIBUTE

TIMEOUT_S = 15
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
            return Outcome(response.status, True, True, "", accepted_conversation(reply))
    except urllib.error.HTTPError as error:
        body = error.read(500).decode("utf-8", "replace")
        final = error.code < 500 and error.code not in RETRYABLE
        return Outcome(error.code, False, final, body)
    except (urllib.error.URLError, OSError) as error:
        return Outcome(0, False, False, type(error).__name__)


def accepted_conversation(body: bytes) -> str:
    """Returns the conversation uuid from Emet's 202 body, or "" when it has none."""
    try:
        accepted = json.loads(body)
        return str(uuid.UUID(str(accepted["conversation"])))
    except (ValueError, TypeError, KeyError, RecursionError):
        return ""


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
    so batches stay in order and one unreachable endpoint costs one timeout, not one per turn."""

    def __init__(self, root: Path, config: Config) -> None:
        self.root = root
        self.config = config
        self.blocked = False
        # Every envelope Emet accepted in this run, queued ones included
        self.accepted: List[Accepted] = []
        self.failures: List[Outcome] = []

    def record(self, envelope: Dict[str, Any], outcome: Outcome) -> None:
        if outcome.delivered:
            resource = envelope.get("resource")
            repository = str(resource.get(REPOSITORY_ATTRIBUTE) or "")[:REPOSITORY_MAX] if isinstance(resource, dict) else ""
            self.accepted.append(Accepted(str(envelope.get("conversation_id") or ""), repository, outcome))
        else:
            self.failures.append(outcome)

    def send(self, envelopes: List[Dict[str, Any]]) -> None:
        for envelope in envelopes:
            if self.blocked:
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
                envelope = storage.read_json(path, None)
                if not isinstance(envelope, dict):
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
