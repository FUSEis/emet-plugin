"""Emet's id of each finished turn a session delivered, so its report can be read later.

One file per session, a JSON object per line. Lines are only ever added, so another process can
read the file while a worker is still sending.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List

from . import storage

# Any worker writes this file, for a session id read from a queued envelope
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def path(root: Path, session_id: str) -> Path:
    return root / "state" / f"{session_id}.pending.jsonl"


def lines(root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Returns the readable lines, oldest first. A line cut short by a crash is skipped."""
    try:
        text = path(root, session_id).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    found = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError, RecursionError):
            item = json.loads(line)
            if isinstance(item, dict):
                found.append(item)
    return found


def traces(root: Path, session_id: str) -> List[str]:
    """Returns the id of each kept turn once, oldest first."""
    found: List[str] = []
    for item in lines(root, session_id):
        # The value goes into a request path later, so it is checked again on the way out
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            trace_uuid = str(uuid.UUID(item.get("trace_uuid")))
            if trace_uuid not in found:
                found.append(trace_uuid)
    return found


def append(root: Path, session_id: str, item: Dict[str, str], once: str = "") -> None:
    """Adds one line. Skipped when once is a trace already kept. Never raises: a send must not fail over this file."""
    if not SESSION_ID.fullmatch(session_id):
        return
    # The lock is held for this one write only, never across a request
    with contextlib.suppress(OSError), storage.locked(root / "state" / f"{session_id}.pending.lock"):
        if once and once in traces(root, session_id):
            return
        fd = os.open(str(path(root, session_id)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item) + "\n")


def note(root: Path, session_id: str, trace_uuid: str) -> None:
    """Keeps the id of a turn Emet accepted in full."""
    append(root, session_id, {"trace_uuid": trace_uuid, "at": storage.now()}, once=trace_uuid)


@contextlib.contextmanager
def finishing(root: Path, session_id: str, turn_ended: bool) -> Iterator[None]:
    """Stamps the end of a run that followed a finished turn, failed or not, so a reader knows no more ids are coming."""
    try:
        yield
    finally:
        if turn_ended:
            append(root, session_id, {"finished": storage.now()})
