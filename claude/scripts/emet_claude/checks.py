"""The outcomes of the report check: one line for each in state/<session_id>.checks.jsonl, and the last of them in the status file.

Each line of the checks file holds one of these words under "outcome". A final outcome ends the
polling of its turn. A turn without one is polled again after a later Stop.

| Outcome | Final | Meaning |
| --- | --- | --- |
| `flagged` | yes | Emet's report disputes a claim. "handed" says whether Claude Code was given the text. |
| `clarified` | yes | Emet's report, shown by Emet as Clarified. Claude is not woken. |
| `clear` | yes | Emet's report, shown by Emet as Verified. |
| `unchecked` | yes | Emet's report, shown by Emet as Skipped. |
| `nothing` | yes | Emet's report, shown by Emet as Skipped. |
| `unreadable` | yes | A report came that this version of the plugin cannot read, or with a status it does not know. |
| `not_assessed` | yes | Emet answered that it did not assess the turn (404 `not_assessed`). |
| `failed` | yes | Emet answered that its check of the turn failed (404 `failed`). |
| `not_found` | yes | Emet answered that it has no trace with this id (404 `not_found`). |
| `expired` | yes | The turn was delivered more than a day ago and still has no report. It is no longer asked for. |
| `wake_turn` | yes | The turn Claude took after this plugin woke it with a report. It is sent to Emet, and its report is never asked for. |
| `own_command` | yes | A turn a command of this plugin started, such as `/emet:status`. It is sent, and its report is never asked for. |
| `shown` | yes | The prompt hook (`missed.py`) gave this turn's flagged report to Claude Code with the prompt "prompt_id" names. |
| `timed_out` | no | The report was not ready within the 300 seconds of the run. |
| `key_refused` | no | Emet refused the key on a report read (401 or 403), and the run stopped. |
| `rate_limited` | no | Emet answered 429 to a report read. The run stopped and paused the report reads of every session. |
| `paused` | no | A pause was in force, so no report was asked for: Emet could not be reached, or it limited the reads. |
| `not_configured` | no | The Stop was not polled: the key, the team or the endpoint is not set right. |
| `old_claude_code` | no | The Stop was not polled: Claude Code is older than 2.1.287, or its version could not be read. |
| `no_person` | no | The Stop was not polled: no person is at the session (`claude -p`, the SDK). |

A `flagged` line that was not handed over names the reason under "held": `shown_turn` (the turn
began with a prompt that carried an earlier verdict), `scheduled_turn` (Claude Code started the turn
on a schedule), `wake_cap` (the session already had 3 wakes), `newer_turn` (a later turn had started
or been delivered) or `late` (the report was ready more than 60 seconds after its turn ended).
The first of the five that applies is the one named.

Claude's answer in a `shown_turn` quotes the claims of the earlier verdict, and Emet flags such an
answer again. So its report is read and recorded, and Claude is never told of it: no wake, and no
verdict is saved.

No one reads along at a `scheduled_turn` (a `/loop` iteration, a scheduled wake-up), and a wake
there would spend a turn and one of the session's wakes. So its verdict is saved and no wake is
made: the next prompt of the session carries it, which the loop's next iteration is.

A turn is still polled after those 60 seconds, until the 300 are over. A flagged report that
comes then wakes no one. It is read so that it can be saved and recorded, for the next prompt
to carry it.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set

from . import pause, pending, report, storage, timestamps

# Why a flagged report was not handed to Claude
SHOWN_TURN = "shown_turn"
SCHEDULED_TURN = "scheduled_turn"
CAPPED = "wake_cap"
NEWER_TURN = "newer_turn"
LATE = "late"


# Checks listed in the status file
RECENT_CHECKS = 5


FLAGGED = report.FLAGGED
# Every word report.status can return, each recorded as the outcome of a report that is ready
REPORT_STATUSES = (FLAGGED, "clarified", "clear", "unchecked", "nothing")
UNREADABLE = "unreadable"
EXPIRED = "expired"
TIMED_OUT = "timed_out"
KEY_REFUSED = "key_refused"
RATE_LIMITED = "rate_limited"
PAUSED = "paused"
# Why a Stop was not polled at all
NOT_CONFIGURED = "not_configured"
OLD_CLAUDE_CODE = "old_claude_code"
NO_PERSON = "no_person"
# The turn after a wake: sent, and never polled
WAKE_TURN = "wake_turn"
# A turn a command of this plugin started: sent, and never polled. Its answer lists the plugin's own files
OWN_COMMAND = "own_command"
# The line written in place of a report read, for each outcome of a turn that is never polled
NEVER_POLLED = {WAKE_TURN: "was started by a wake", OWN_COMMAND: "was started by a command of the plugin"}
# A flagged verdict given to Claude Code with a later prompt. The prompt hook records it
SHOWN = "shown"
# Emet's 404 codes that end the wait for a report
FINAL_MISSES = ("not_assessed", "failed", "not_found")
# Every outcome a check can hold, with whether it ends the polling of its turn. The table above says what each means
OUTCOMES: Dict[str, bool] = {
    **dict.fromkeys(REPORT_STATUSES, True),
    UNREADABLE: True,
    **dict.fromkeys(FINAL_MISSES, True),
    EXPIRED: True,
    WAKE_TURN: True,
    OWN_COMMAND: True,
    SHOWN: True,
    TIMED_OUT: False,
    KEY_REFUSED: False,
    RATE_LIMITED: False,
    PAUSED: False,
    NOT_CONFIGURED: False,
    OLD_CLAUDE_CODE: False,
    NO_PERSON: False,
}


def checks_path(root: Path, session_id: str) -> Path:
    return root / "state" / f"{session_id}.checks.jsonl"


def checks(root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Returns the outcomes recorded for the session, oldest first. A line cut short by a crash is skipped."""
    try:
        # No link is followed, and a pipe in the file's place cannot make the read wait: one with nothing in it reads as None.
        # An opener, so that a folder in the file's place leaves no descriptor open
        with open(checks_path(root, session_id), "rb", opener=lambda name, _: os.open(name, storage.READ_FLAGS)) as handle:
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


def settled(found: List[Dict[str, Any]]) -> Set[str]:
    """Returns the traces that need no more requests. Only a line with an outcome that is final counts, whatever else it says."""
    return {
        item["trace_uuid"]
        for item in found
        if item.get("final") is True and isinstance(item.get("trace_uuid"), str) and final_outcome(item.get("outcome"))
    }


def final_outcome(outcome: Any) -> bool:
    return isinstance(outcome, str) and OUTCOMES.get(outcome) is True


def wakes(found: List[Dict[str, Any]]) -> int:
    """Counts the reports handed to Claude in this session."""
    return sum(1 for item in found if item.get("handed") is True and item.get("outcome") == FLAGGED)


def shown_prompts(found: List[Dict[str, Any]]) -> Set[str]:
    """Returns Claude Code's id of each prompt of the session that carried a saved verdict."""
    return {pending.plain(item.get("prompt_id")) for item in found if item.get("outcome") == SHOWN} - {""}


def handed_times(root: Path, session_id: str) -> List[float]:
    """Returns when each report was handed to Claude in this session. The sender takes a turn for one a wake started only after one."""
    found = [timestamps.parse(item.get("at")) for item in checks(root, session_id) if wakes([item])]
    return [at.timestamp() for at in found if at is not None]


def known(found: List[Dict[str, Any]], trace_uuid: str, report_uuid: str, revision: int) -> bool:
    """Says whether this revision of the trace's report was already recorded as flagged. Emet can check a trace again."""
    key = (trace_uuid, report_uuid, revision)
    return any(
        item.get("outcome") == FLAGGED and (item.get("trace_uuid"), item.get("report_uuid"), item.get("revision")) == key for item in found
    )


def record(root: Path, session_id: str, trace_uuid: str, outcome: str, ids: Optional[Mapping[str, Any]] = None, wait: bool = True) -> bool:
    """Adds one outcome to the session's file and the status file. Returns False when the session's file was not written.

    Only an outcome of OUTCOMES can be recorded. Without wait the status file is left alone when another process is writing it.
    """
    item = {"trace_uuid": trace_uuid, "outcome": outcome, "final": OUTCOMES[outcome], "at": storage.now(pause.clock()), **(ids or {})}
    # No report is read for such a turn or for a showing, and the line must not look as if one was
    line = (
        f"turn {trace_uuid} {NEVER_POLLED[outcome]}, not polled"
        if outcome in NEVER_POLLED
        else f"verdict {trace_uuid} shown with a prompt"
        if outcome == SHOWN
        else f"report {trace_uuid} -> {outcome}"
    )
    target = checks_path(root, session_id)
    try:
        # A link in the file's place is removed, never written through. The flags below close the gap after this look
        if target.is_symlink():
            target.unlink()
            storage.log(root, "checks file was a link, removed")
        written = b""
        if target.exists():
            with open(target, "rb", opener=lambda name, _: os.open(name, storage.READ_FLAGS)) as handle:
                written = handle.read()
        # A write cut short by a crash leaves no newline, which would swallow this line too
        start = "" if not written or written.endswith(b"\n") else "\n"
        # The flags of a read refuse a link and never wait for a pipe. They hold no access mode, which is added here
        fd = os.open(str(target), storage.READ_FLAGS | os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(start + json.dumps(item) + "\n")
    except OSError as error:
        storage.log(root, f"{line}, not recorded {type(error).__name__}")
        return False
    storage.log(root, line)
    show(root, {"session_id": session_id, **item}, wait)
    return True


def show(root: Path, check: Optional[Dict[str, Any]] = None, wait: bool = True) -> None:
    """Lists the check in the status file, newest first and once per trace, with the pause of the report reads. Never raises."""
    lock = root / "status.lock"
    try:
        with storage.locked(lock) if wait else storage.try_locked(lock) as held:
            if held is False:
                storage.log(root, "check not shown, the status file is in use")
                return
            status = storage.read_json(root / "status.json", {})
            if not isinstance(status, dict):
                status = {}
            if check is not None:
                saved = status.get("checks")
                earlier = saved if isinstance(saved, list) else []
                others = [item for item in earlier if isinstance(item, dict) and item.get("trace_uuid") != check["trace_uuid"]]
                status["checks"] = [check, *others][:RECENT_CHECKS]
            # A time only. The sender's own pause is shown by the sender
            status["reads_paused_until"] = pause.until(root, pause.READS)
            storage.write_json(root / "status.json", status)
    except OSError as error:
        storage.log(root, f"check not shown {type(error).__name__}")
