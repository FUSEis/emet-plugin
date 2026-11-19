"""Emet's id of each finished turn a session delivered, so its report can be read later.

One file per session, a JSON object per line. Lines are only ever added, so another process can
read the file while a worker is still sending. A trace line names the endpoint that accepted the
turn, and Emet's id of its conversation when the reply gave one. A finished line ends one run and
counts the envelopes still in the retry queue: while that is above zero, a later run of any session
can still add an id here. A skipped line marks a turn or a later addition that could not be built
and was never sent, with the error's type as its reason. A refused line names a turn Emet refused
for good, by the id the plugin sent it under, with the HTTP status and the endpoint that refused it.
It holds no trace_uuid, so it is never read as a report to fetch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set, Union

from . import storage

# Any worker writes this file, for a session id read from a queued envelope
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def path(root: Path, session_id: str) -> Path:
    return root / "state" / f"{session_id}.pending.jsonl"


def lines(root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Returns the readable lines, oldest first. A line cut short by a crash is skipped."""
    if not SESSION_ID.fullmatch(session_id):
        return []
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


def append(root: Path, session_id: str, item: Dict[str, Union[str, int]], once: str = "") -> None:
    """Adds one line. Skipped when once is a trace already kept. Never raises: a send must not fail over this file."""
    if not SESSION_ID.fullmatch(session_id):
        storage.log(root, "pending not written: the session id is not a plain name")
        return
    target = path(root, session_id)
    try:
        # The lock is held for this one write only, never across a request
        with storage.locked(root / "state" / f"{session_id}.pending.lock"):
            if once and once in traces(root, session_id):
                return
            written = target.read_bytes() if target.exists() else b""
            # A write cut short by a crash leaves no newline, which would swallow this line too
            start = "" if not written or written.endswith(b"\n") else "\n"
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(start + json.dumps(item) + "\n")
    except OSError as error:
        storage.log(root, f"pending not written: {type(error).__name__}")


def note(root: Path, session_id: str, trace_uuid: str, endpoint: str, conversation_uuid: str = "") -> None:
    """Keeps the id of a turn whose last batch Emet accepted, with Emet's id of its conversation when the reply gave one."""
    item: Dict[str, Union[str, int]] = {"trace_uuid": trace_uuid, "at": storage.now(), "endpoint": endpoint}
    if conversation_uuid:
        item["conversation_uuid"] = conversation_uuid
    append(root, session_id, item, once=trace_uuid)


def refuse(root: Path, session_id: str, trace_id: str, status: int, endpoint: str) -> None:
    """Marks a turn Emet refused for good. Written by the worker that got the answer, for the session the turn belongs to."""
    append(root, session_id, {"refused": trace_id, "at": storage.now(), "status": status, "endpoint": endpoint})


def refused(root: Path, session_id: str) -> Set[str]:
    """Returns the ids of the session's turns Emet refused for good, as the plugin sent them."""
    return {item["refused"] for item in lines(root, session_id) if isinstance(item.get("refused"), str) and item["refused"]}


@contextlib.contextmanager
def finishing(root: Path, session_id: str, turn_ended: bool) -> Iterator[None]:
    """Stamps the end of a run that followed a finished turn, failed or not, so a reader knows this run adds no more ids."""
    try:
        yield
    finally:
        if turn_ended:
            # Counted for every session: reading each queued envelope to find its session would be slow
            queued = len(list((root / "queue").glob("*.json")))
            append(root, session_id, {"finished": storage.now(), "queued": queued})
