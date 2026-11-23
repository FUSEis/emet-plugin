"""What a session's own files say about its turns: the end of Claude Code's
session file, and the lines the sender kept for the turns Emet accepted.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from . import pending, storage, timestamps, transcript

# Read from the environment as a second sign of a session with no person at it. Not a plugin option
ENTRYPOINT_NAME = "CLAUDE_CODE_ENTRYPOINT"
VERSION_FORM = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}")


# How much of the end of the session file is read, and how far past the point where the poller started
TAIL_BYTES = 1024 * 1024
AHEAD_BYTES = 1024 * 1024


# Keys that mark a pending line as something other than a delivered turn
NOT_A_TRACE = ("finished", "skipped", "refused")


@dataclass(frozen=True)
class Trace:
    """A delivered turn, by Emet's ids."""

    trace_uuid: str
    at: float
    conversation_uuid: str
    # Claude Code's id of the turn and the time of its last entry, "" and 0 for a line that names none
    prompt_id: str = ""
    ended: float = 0.0
    # Whether Claude Code started the turn on a schedule, as the sender marked its line
    scheduled: bool = False


def started_by_hook(hook_input: Mapping[str, Any]) -> bool:
    """Says whether the turn that just ended was started by a Stop hook, such as the one a wake starts."""
    return hook_input.get("stop_hook_active") is True


def headless(found: List[Tuple[int, Dict[str, Any]]], environ: Mapping[str, str]) -> bool:
    """Says whether no person is at the session (claude -p, the SDK). There a wake is ignored and this hook holds up the exit.

    The environment can only add a reason to say yes: whether Claude Code sets the name for a hook is not confirmed.
    """
    if environ.get(ENTRYPOINT_NAME, "").startswith("sdk"):
        return True
    entries = [entry for _, entry in found]
    # A scheduled fire or a message from another session says nothing of who is at the session
    starts = [entry for entry in entries if transcript.starts_turn(entry) and not entry.get("isMeta")]
    if starts and "sdk" in (starts[-1].get("promptSource"), starts[-1].get("turnOrigin")):
        return True
    named = [entry["entrypoint"] for entry in entries if isinstance(entry.get("entrypoint"), str)]
    return bool(named) and named[-1].startswith("sdk")


def session_entries(transcript_path: str, around: Optional[int] = None) -> Tuple[List[Tuple[int, Dict[str, Any]]], int]:
    """Returns (byte offset, entry) for the readable lines near the end of the session file, and the file's size.

    With around, the lines near that offset: the end of the turn before it and the start of what followed.
    """
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            begin = max(min(size if around is None else around, size) - TAIL_BYTES, 0)
            handle.seek(begin)
            data = handle.read(TAIL_BYTES + AHEAD_BYTES)
    except OSError:
        return [], 0
    found = []
    position = begin
    for raw in data.split(b"\n"):
        # The first line can be cut by the seek, and the last one still be in writing
        with contextlib.suppress(ValueError, RecursionError):
            entry = storage.loads(raw)
            if isinstance(entry, dict):
                found.append((position, entry))
        position += len(raw) + 1
    return found, size


def last_time(found: List[Tuple[int, Dict[str, Any]]]) -> float:
    """Returns the time of the last entry that names one, or 0. Taken from the file, so a late start of this process does not move it."""
    for _, entry in reversed(found):
        at = timestamps.parse(entry.get("timestamp"))
        if at is not None:
            # Milliseconds, as the Stop file and a trace line hold a time, so every path compares the same number
            return at.replace(microsecond=at.microsecond // 1000 * 1000).timestamp()
    return 0.0


def claude_code_version(found: List[Tuple[int, Dict[str, Any]]]) -> Tuple[int, ...]:
    """Returns the Claude Code version the last entry of the session file names, or () when none does."""
    for _, entry in reversed(found):
        named = entry.get("version")
        match = VERSION_FORM.match(named) if isinstance(named, str) else None
        if match:
            return tuple(int(part) for part in match.group(0).split("."))
    return ()


def turn_starts(found: List[Tuple[int, Dict[str, Any]]]) -> List[Tuple[int, Dict[str, Any]]]:
    """Returns the entries that start a turn a model call answers, grouped as the sender groups them."""
    main = [item for item in found if not item[1].get("isSidechain")]
    return [
        item
        for item, opens in zip(main, transcript.opens_turn(entry for _, entry in main))
        # A command of the plugin is in the same wrapper as a local command, and a model call answers it
        if opens and (not transcript.LOCAL_COMMAND.match(transcript.text_of(item[1])) or transcript.own_command(item[1]))
    ]


def newer_trace(root: Path, endpoint: str, session_id: str, trace: Trace) -> bool:
    """Says whether a later turn of the session has been delivered. A turn one of the plugin's commands started is one too."""
    found = delivered(person_turns(pending.lines(root, session_id)), endpoint)
    ids = [other.trace_uuid for other in found]
    after = ids[ids.index(trace.trace_uuid) + 1 :] if trace.trace_uuid in ids else ids
    for other in found:
        if other.trace_uuid == trace.trace_uuid:
            continue
        # The retry queue can deliver an older turn after a newer one, so the turns' own ends decide when both name one
        if trace.ended and other.ended:
            if other.ended > trace.ended:
                return True
        elif other.trace_uuid in after:
            return True
    return False


def own_command_turns(lines: List[Dict[str, Any]]) -> Set[str]:
    """Returns the traces of the turns a command of this plugin started. Their reports are never read."""
    return {
        item["trace_uuid"]
        for item in lines
        if item.get(pending.STARTED_BY) == pending.OWN_COMMAND and isinstance(item.get("trace_uuid"), str)
    }


def person_turns(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Returns the lines with the mark of a command turn taken off: the person who ran the command has moved on, unlike after a wake."""
    return [
        {key: value for key, value in item.items() if key != pending.STARTED_BY}
        if item.get(pending.STARTED_BY) == pending.OWN_COMMAND
        else item
        for item in lines
    ]


def delivered(lines: List[Dict[str, Any]], endpoint: str, by_plugin: bool = False) -> List[Trace]:
    """Returns the turns delivered to this endpoint, once each, oldest first. Any other line is not a trace to poll.

    With by_plugin, the turns a hook of this plugin started instead. Those are never polled.
    """
    found: Dict[str, Trace] = {}
    for item in lines:
        if any(key in item for key in NOT_A_TRACE) or item.get("endpoint") != endpoint:
            continue
        if (pending.STARTED_BY in item) != by_plugin:
            continue
        at = timestamps.parse(item.get("at"))
        if at is None:
            continue
        # The id goes into a request path
        try:
            trace_uuid = str(uuid.UUID(item.get("trace_uuid")))
        except (ValueError, TypeError, AttributeError):
            continue
        ended = timestamps.parse(item.get("ended"))
        conversation = ""
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            # Kept in one form: it is written into a saved verdict, which is read back only with ids as the plugin writes them
            conversation = str(uuid.UUID(item.get("conversation_uuid")))
        found.setdefault(
            trace_uuid,
            Trace(
                trace_uuid,
                at.timestamp(),
                conversation,
                pending.plain(item.get("prompt_id")),
                ended.timestamp() if ended else 0.0,
                item.get(pending.TURN_ORIGIN) == pending.SCHEDULED,
            ),
        )
    return list(found.values())


def sender_done(lines: List[Dict[str, Any]], start: float) -> bool:
    """Says whether a run of the sender ended since start with an empty retry queue, so no more ids can follow.

    With envelopes still queued a later run of any session can add an id, so the poller keeps waiting.
    """
    for item in lines:
        at = timestamps.parse(item.get("finished"))
        # Cut to whole seconds: a run stamped in the second the poller started in is an earlier Stop's.
        # The sender of this Stop waits a second before it sends
        if at is not None and int(at.timestamp()) > int(start) and type(item.get("queued")) is int and item["queued"] == 0:
            return True
    return False
