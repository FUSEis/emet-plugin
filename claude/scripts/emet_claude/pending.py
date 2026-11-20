"""Emet's id of each finished turn a session delivered, so its report can be read later.

One file per session, a JSON object per line. Lines are only ever added, so another process can
read the file while a worker is still sending. A trace line names the endpoint that accepted the
turn, and Emet's id of its conversation when the reply gave one. A finished line ends one run and
counts the envelopes still in the retry queue: while that is above zero, a later run of any session
can still add an id here. A skipped line marks a turn or a later addition that could not be built
and was never sent, with the error's type as its reason. A refused line names a turn Emet refused
for good, by the id the plugin sent it under, with the HTTP status and the endpoint that refused it.
It holds no trace_uuid, so it is never read as a report to fetch. A trace line with started_by
names a turn that this plugin started or was asked for: the turn after a wake, or a turn one of
its own commands started. Its report is never read. A trace line also names the turn itself when the sender could: prompt_id is Claude
Code's id of the turn, and ended is the time of the turn's last entry, in UTC with milliseconds.
Both stay on this machine. The poller tells by them which turn a report is about.
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
# The key that marks a trace line as a turn a hook of this plugin started
STARTED_BY = "started_by"
# Its value for a turn one of the plugin's own commands started, such as /emet:status
OWN_COMMAND = "own_command"


def plain(value: Any) -> str:
    """Returns an id when it is a plain name, else "". Ids are written to files and compared, never trusted."""
    return value if isinstance(value, str) and SESSION_ID.fullmatch(value) else ""


def path(root: Path, session_id: str) -> Path:
    return root / "state" / f"{session_id}.pending.jsonl"


def lines(root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Returns the readable lines, oldest first. A line cut short by a crash is skipped."""
    if not SESSION_ID.fullmatch(session_id):
        return []
    try:
        # No link is followed, and a pipe in the file's place cannot make the read wait: one with nothing in it reads as None
        with os.fdopen(os.open(str(path(root, session_id)), storage.READ_FLAGS), "rb") as handle:
            text = (handle.read() or b"").decode("utf-8")
    except (OSError, ValueError):
        return []
    found = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError, RecursionError):
            item = storage.loads(line)
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
            # Windows has no flag that refuses a link at the open, so this look is the guard there
            if not storage.NOFOLLOW and target.is_symlink():
                raise OSError("a link")
            written = b""
            if target.exists():
                with os.fdopen(os.open(str(target), storage.READ_FLAGS), "rb") as handle:
                    written = handle.read()
            # A write cut short by a crash leaves no newline, which would swallow this line too
            start = "" if not written or written.endswith(b"\n") else "\n"
            # The flags of a read refuse a link and never wait for a pipe. They hold no access mode, which is added here
            fd = os.open(str(target), storage.READ_FLAGS | os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(start + json.dumps(item) + "\n")
    except OSError as error:
        storage.log(root, f"pending not written: {type(error).__name__}")


def note(
    root: Path,
    session_id: str,
    trace_uuid: str,
    endpoint: str,
    conversation_uuid: str = "",
    started_by: str = "",
    prompt_id: str = "",
    ended: str = "",
) -> None:
    """Keeps the id of a turn Emet accepted, with Emet's id of its conversation when the reply gave one.

    started_by names what of this plugin started the turn, a hook or a command, and marks the line as one whose report is never read.
    prompt_id and ended name the turn itself: Claude Code's id of it and the time of its last entry.
    """
    item: Dict[str, Union[str, int]] = {"trace_uuid": trace_uuid, "at": storage.now(), "endpoint": endpoint}
    if conversation_uuid:
        item["conversation_uuid"] = conversation_uuid
    if started_by:
        item[STARTED_BY] = started_by
    if prompt_id:
        item["prompt_id"] = prompt_id
    if ended:
        item["ended"] = ended
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
