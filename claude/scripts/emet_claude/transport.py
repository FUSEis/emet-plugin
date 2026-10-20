"""Posts envelopes to Emet and keeps the ones that could not be delivered for a later retry."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from . import VERSION, storage
from .config import Config

TIMEOUT_S = 15
# Bounds the queue so an unreachable endpoint cannot fill the disk
QUEUE_MAX_FILES = 500
QUEUE_MAX_BYTES = 256 * 1024 * 1024
RETRYABLE = (408, 425, 429)


@dataclass(frozen=True)
class Outcome:
    status: int
    delivered: bool
    final: bool
    detail: str


def post(config: Config, envelope: Dict[str, Any]) -> Outcome:
    """Sends one envelope. final is True when resending would not change the answer."""
    request = urllib.request.Request(
        f"{config.endpoint}/api/v1/traces/",
        data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"emet-claude-code/{VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return Outcome(response.status, True, True, "")
    except urllib.error.HTTPError as error:
        body = error.read(500).decode("utf-8", "replace")
        final = error.code < 500 and error.code not in RETRYABLE
        return Outcome(error.code, False, final, body)
    except (urllib.error.URLError, OSError) as error:
        return Outcome(0, False, False, type(error).__name__)


def queue_name(envelope: Dict[str, Any]) -> str:
    first = envelope["spans"][0]["span_id"] if envelope.get("spans") else ""
    key = f"{envelope.get('trace_id')}/{first}".encode()
    return hashlib.sha256(key).hexdigest()[:24] + ".json"


def enqueue(root: Path, envelope: Dict[str, Any]) -> None:
    queue = root / "queue"
    storage.write_json(queue / queue_name(envelope), envelope)
    files = sorted(queue.glob("*.json"), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in files)
    while files and (len(files) > QUEUE_MAX_FILES or total > QUEUE_MAX_BYTES):
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink()
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

    def send(self, envelopes: List[Dict[str, Any]]) -> List[Outcome]:
        outcomes: List[Outcome] = []
        for envelope in envelopes:
            if self.blocked:
                enqueue(self.root, envelope)
                continue
            outcome = post(self.config, envelope)
            outcomes.append(outcome)
            storage.log(
                self.root, f"trace {envelope['trace_id']} spans={len(envelope['spans'])} end={envelope['end']} -> {describe(outcome)}"
            )
            if not outcome.delivered and not outcome.final:
                enqueue(self.root, envelope)
                self.blocked = True
        return outcomes

    def flush(self) -> int:
        """Retries queued envelopes, oldest first. Returns how many are still queued."""
        files = sorted((self.root / "queue").glob("*.json"), key=lambda path: path.stat().st_mtime)
        for index, path in enumerate(files):
            envelope = storage.read_json(path, None)
            if not isinstance(envelope, dict):
                path.unlink()
                continue
            outcome = post(self.config, envelope)
            storage.log(self.root, f"retry {envelope.get('trace_id')} -> {describe(outcome)}")
            if not outcome.delivered and not outcome.final:
                self.blocked = True
                return len(files) - index
            path.unlink()
        return 0
