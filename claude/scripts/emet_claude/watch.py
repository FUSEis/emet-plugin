"""Reads the Emet report of each delivered turn after the turn ends, and wakes Claude when a report is flagged.

Runs as its own background hook, started at every Stop, one at a time per session. Every polled
Stop writes its turn to state/<session_id>.stop.json, so a watcher already running judges that
turn by its own Stop. The Stop that found a watcher waits to take over. It exits 0 whatever
happens, except when it hands a flagged report to Claude. Each outcome is added as a line to
state/<session_id>.checks.jsonl and shown in the status file. A flagged verdict is saved to its
own file under verdicts/ before Claude is woken. Only that file holds text from a report.

Exit code 2 hands the text to Claude Code. It does not prove Claude told the user: a wake that
lands inside a running turn can go unseen. So the verdict file is kept either way, until the
prompt hook (`missed.py`) finds the wake in the session file or shows the verdict with a prompt.

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
began with a prompt that carried an earlier verdict), `wake_cap` (the session already had 3 wakes),
`newer_turn` (a later turn had started or been delivered) or `late` (the report was ready
more than 60 seconds after its turn ended). The first of the four that applies is the one named.

Claude's answer in a `shown_turn` quotes the claims of the earlier verdict, and Emet flags such an
answer again. So its report is read and recorded, and Claude is never told of it: no wake, and no
verdict is saved.

A turn is still polled after those 60 seconds, until the 300 are over. A flagged report that
comes then wakes no one. It is read so that it can be saved and recorded, for the next prompt
to carry it.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import random
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from . import VERSION, pause, pending, report, storage, timestamps, transcript, transport
from .config import Config, endpoint_problem, load

# The first Claude Code that reports a waking hook it cannot start once, not at every turn. The README requires it
MIN_CLAUDE_CODE = (2, 1, 287)
# Read from the environment as a second sign of a session with no person at it. Not a plugin option
ENTRYPOINT_NAME = "CLAUDE_CODE_ENTRYPOINT"
VERSION_FORM = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}")
UUID_FORM = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The name of a saved verdict: the session id, the trace and the revision of the report
VERDICT_NAME = re.compile(rf"[A-Za-z0-9_-]{{1,100}}\.{UUID_FORM.pattern}\.[0-9]{{1,7}}\.json")
# The name storage.write_private gives a verdict while it is written
VERDICT_TEMP_NAME = re.compile(rf"\.{VERDICT_NAME.pattern}\.[0-9]{{1,10}}\.tmp")
# How much of the end of the session file is read, and how far past the point where the poller started
TAIL_BYTES = 1024 * 1024
AHEAD_BYTES = 1024 * 1024
# Seconds from the end of the turn to the first request, then the waits between requests; the last one repeats
FIRST_S = 8
STEPS_S = (5, 10, 15, 20, 30)
# No request starts after this many seconds
BUDGET_S = 300
# The process ends itself here, below the hook's own limit, even when a request or a lock hangs
HARD_STOP_S = 320
# The time the hook that runs the poller must be given, above the hard stop
HOOK_TIMEOUT_S = 330
# How long the poller waits for the sender with nothing to poll
IDLE_S = 60
# How often the pending file is read again while waiting
TICK_S = 2.0
SHORTEST_SLEEP_S = 0.05
# Tries to write the session's latest Stop, and the wait between them
STOP_WRITES = 5
STOP_WRITE_WAIT_S = 0.05
# A temporary file of a verdict left by a write cut short is removed once it is this old
STALE_TEMP_S = 24 * 3600
REQUEST_TIMEOUT_S = 10
REPORT_MAX_BYTES = 2 * 1024 * 1024
# The largest share added to a wait by chance
EXTRA = 0.1
# Wakes per session; later flagged reports are saved and recorded only
WAKE_CAP = 3
# Seconds after its turn ended that a report can still wake Claude. A starting value to tune on production timings
LATE_S = 60
# How long before that window closes its last read starts, which leaves the request time to be answered
EDGE_S = 1
# Why a flagged report was not handed to Claude
SHOWN_TURN = "shown_turn"
CAPPED = "wake_cap"
NEWER_TURN = "newer_turn"
LATE = "late"
# Turns polled at once, newest first, so a long backlog cannot become a burst of requests
MAX_WATCHED = 5
# A turn delivered longer ago than this is no longer polled
MAX_AGE_S = 24 * 3600
VERDICT_DAYS = 7
# The formats of the Stop file and of a saved verdict. A file that names no version is read as 1
STOP_VERSION = 1
VERDICT_VERSION = 1
# Far above any verdict save_verdict writes. A longer file is not read
VERDICT_MAX_BYTES = 64 * 1024
# How far ahead of the clock a saved verdict's time may be, for a clock that was set back
CLOCK_SLACK_S = 300
# Why a saved verdict is not shown, as the log names it. Nothing else of the file is logged
BAD_NAME = "name"
A_LINK = "link"
TOO_LONG = "size"
NOT_READ = "unreadable"
BAD_VERSION = "version"
OTHER_WORDING = "wording"
BAD_SHAPE = "shape"
OTHER_NAME = "mismatch"
BAD_TEXT = "text"
BAD_AGE = "age"
# Checks listed in the status file
RECENT_CHECKS = 5
WAKE_EXIT = 2

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
# Keys that mark a pending line as something other than a delivered turn
NOT_A_TRACE = ("finished", "skipped", "refused")


@dataclass(frozen=True)
class Answer:
    """What Emet answered a report read with. status is 0 when it could not be reached."""

    status: int
    code: str = ""
    document: Any = None
    retry_after: Optional[float] = None


@dataclass(frozen=True)
class Trace:
    """A delivered turn, by Emet's ids."""

    trace_uuid: str
    at: float
    conversation_uuid: str
    # Claude Code's id of the turn and the time of its last entry, "" and 0 for a line that names none
    prompt_id: str = ""
    ended: float = 0.0


@dataclass(frozen=True)
class Stop:
    """The Stop this run follows."""

    session_id: str
    transcript_path: str
    # Claude Code's id of the turn that ended, "" when the hook input names none
    prompt_id: str
    # The size of the session file when the run started
    size: int
    # When the run started, and a name of its own that tells it from the other Stops of the session
    at: float = 0.0
    token: str = ""
    # The time of the last entry the turn had written when the run started, 0 when none could be read
    ended: float = 0.0


@dataclass
class Plan:
    """When a turn's report is asked for next, and how many times it was not ready."""

    due: float
    step: int = 0


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def extra() -> float:
    """Returns the share added to a wait. Never negative: a wait is only ever made longer."""
    return random.uniform(0, EXTRA)  # noqa: S311 - timing, not a secret


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
    starts = [entry for entry in entries if transcript.starts_turn(entry)]
    if starts and "sdk" in (starts[-1].get("promptSource"), starts[-1].get("turnOrigin")):
        return True
    named = [entry["entrypoint"] for entry in entries if isinstance(entry.get("entrypoint"), str)]
    return bool(named) and named[-1].startswith("sdk")


def wake(text: str) -> int:
    """Hands the text to Claude Code and returns the exit code that wakes Claude. Only stderr reaches Claude."""
    # Bytes: a piped stream on Windows can refuse text that is not ASCII
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.buffer.flush()
    return WAKE_EXIT


def hard_stop() -> threading.Timer:
    """Ends the process once the run has outlived its time, whatever it is waiting on."""
    timer = threading.Timer(HARD_STOP_S, os._exit, args=(0,))
    timer.daemon = True
    timer.start()
    return timer


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


def newer_turn(stop: Stop) -> bool:
    """Says whether the session file shows a turn started after the one this Stop ended."""
    found, _ = session_entries(stop.transcript_path, stop.size)
    starts: List[Tuple[int, Dict[str, Any]]] = []
    answered = True
    for offset, entry in found:
        if entry.get("isSidechain"):
            continue
        if entry.get("type") == "assistant":
            answered = True
        if not transcript.starts_turn(entry):
            continue
        # A command of the plugin is in the same wrapper as a local command, and a model call answers it
        if transcript.LOCAL_COMMAND.match(transcript.text_of(entry)) and not transcript.own_command(entry):
            continue
        # As the sender groups turns: a meta start joins the start before it that has no answer yet
        if entry.get("isMeta") and starts and not answered:
            continue
        starts.append((offset, entry))
        answered = False
    if any(offset >= stop.size for offset, _ in starts):
        return True
    if not starts:
        return False
    # A prompt queued during the turn can be in the file before this process starts. Told apart by the turn's id
    ids = [entry.get("promptId") for _, entry in starts]
    if stop.prompt_id and stop.prompt_id in ids:
        return ids[-1] != stop.prompt_id
    # Without the id: the turn that ended was answered before this run started, a queued one is answered after it or not yet
    last = starts[-1][0]
    answers = [entry for offset, entry in found if offset > last and entry.get("type") == "assistant" and not entry.get("isSidechain")]
    at = timestamps.parse(answers[0].get("timestamp")) if answers else None
    return bool(stop.at) and (not answers or (at is not None and at.timestamp() > stop.at))


def stop_path(root: Path, session_id: str) -> Path:
    return root / "state" / f"{session_id}.stop.json"


def announce(root: Path, stop: Stop) -> None:
    """Names this Stop as the session's latest, for the watcher that may already run. Ids and numbers only. Never raises."""
    # A later Stop can have written first. The session file only grows, so the larger size is the later turn
    if latest(root, stop).size > stop.size:
        storage.log(root, "stop not written, a later Stop of the session is already named")
        return
    saved = {
        storage.VERSION_KEY: STOP_VERSION,
        "token": stop.token,
        "prompt_id": stop.prompt_id,
        "size": stop.size,
        "at": storage.now(stop.at),
    }
    if stop.ended:
        saved["ended"] = timestamps.text(datetime.fromtimestamp(stop.ended, timezone.utc))
    for attempt in range(STOP_WRITES):
        try:
            storage.write_json(stop_path(root, stop.session_id), saved)
            return
        except PermissionError as error:
            # Windows refuses to replace a file another process has open. The watcher reads it for a moment only
            if attempt == STOP_WRITES - 1:
                storage.log(root, f"stop not written {type(error).__name__}")
                return
            sleep(STOP_WRITE_WAIT_S)
        except OSError as error:
            storage.log(root, f"stop not written {type(error).__name__}")
            return


def latest(root: Path, own: Stop) -> Stop:
    """Returns the session's latest Stop: the one in its file, or this run's own when the file is damaged or of an earlier Stop.

    A file of a version this code does not know is read as a damaged one.
    """
    saved = storage.read_json(stop_path(root, own.session_id), None)
    if not isinstance(saved, dict) or not storage.known_version(saved, STOP_VERSION):
        return own
    at = timestamps.parse(saved.get("at"))
    token, size, prompt_id = saved.get("token"), saved.get("size"), saved.get("prompt_id")
    # The stamp holds whole seconds
    if at is None or at.timestamp() < int(own.at) or not isinstance(token, str) or token in ("", own.token):
        return own
    # Times hold whole seconds. The size tells an earlier Stop of the same second from a later one
    if type(size) is not int or size < own.size:
        return own
    ended = timestamps.parse(saved.get("ended"))
    return Stop(
        own.session_id, own.transcript_path, pending.plain(prompt_id), size, at.timestamp(), token, ended.timestamp() if ended else 0.0
    )


def earlier_turn(trace: Trace, stop: Stop) -> bool:
    """Says whether the trace is of a turn before the one the Stop ended."""
    if trace.prompt_id and stop.prompt_id:
        return trace.prompt_id != stop.prompt_id
    # Without both ids: a turn is delivered after it ends, so one delivered before the last entry of the latest turn is an earlier one
    return trace.at < int(stop.ended)


def turn_end(trace: Trace, stop: Stop) -> float:
    """Returns when the trace's turn ended: by its own line, else the last entry the Stop read, else the second the Stop began in."""
    return trace.ended or stop.ended or float(int(stop.at))


def hold_reason(root: Path, config: Config, stop: Stop, trace: Trace, found: List[Dict[str, Any]]) -> str:
    """Returns why a flagged report is not handed to Claude, or "" when it is. stop is the session's latest Stop.

    The first reason that applies is the one recorded: a turn that carried a verdict, the cap, then a newer turn, then a late report.
    """
    # Its answer quotes the claims of the verdict it carried. Telling Claude of that would start a chain
    if trace.prompt_id and trace.prompt_id in shown_prompts(found):
        return SHOWN_TURN
    if wakes(found) >= WAKE_CAP:
        return CAPPED
    # A wake that lands in a running turn is folded into it and can go unseen
    if earlier_turn(trace, stop) or newer_trace(root, config.endpoint, stop.session_id, trace) or newer_turn(stop):
        return NEWER_TURN
    # The user has moved on by then. Asked last: only the report of the turn in view can be called late
    if pause.clock() - turn_end(trace, stop) > LATE_S:
        return LATE
    return ""


def before_the_window_closes(due: float, end: float, soonest: float) -> float:
    """Returns when to ask next: as planned, or sooner, so that one read still starts inside the time a wake is allowed in.

    The waits only grow by chance and by the time requests take, so the planned read would often come just after it.
    Never sooner than soonest: the shortest wait, or the one Emet named.
    """
    edge = end + LATE_S - EDGE_S
    return edge if soonest <= edge < due else due


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


def read_report(config: Config, trace_uuid: str) -> Answer:
    """Asks Emet once for the report of one trace. Never raises."""
    request = urllib.request.Request(
        f"{config.endpoint}/api/v1/traces/{trace_uuid}/report/",
        headers={
            "Authorization": f"Bearer {config.emet_key}",
            "Accept": "application/json",
            "User-Agent": f"emet-claude-code/{VERSION}",
        },
        method="GET",
    )
    try:
        with transport.ask(request, REQUEST_TIMEOUT_S) as response:
            if response.status != 200:
                # The body of a 202 is never read, so it can never reach the renderer
                return Answer(response.status, retry_after=pause.named(response.headers.get("Retry-After")))
            return Answer(200, document=parsed(response.read(REPORT_MAX_BYTES + 1)))
    except urllib.error.HTTPError as error:
        body = b""
        if error.code == 404:
            with contextlib.suppress(OSError, http.client.HTTPException):
                body = error.read(transport.MAX_REPLY_BYTES)
        return Answer(error.code, transport.miss_code(body), retry_after=pause.named(error.headers.get("Retry-After")))
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return Answer(0)


def parsed(body: bytes) -> Any:
    """Returns the JSON value of a report body, or None when it is too long or not JSON."""
    if len(body) > REPORT_MAX_BYTES:
        return None
    try:
        return storage.loads(body)
    except (ValueError, RecursionError):
        return None


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
            ),
        )
    return list(found.values())


def sender_done(lines: List[Dict[str, Any]], start: float) -> bool:
    """Says whether a run of the sender ended since start with an empty retry queue, so no more ids can follow.

    With envelopes still queued a later run of any session can add an id, so the poller keeps waiting.
    """
    for item in lines:
        at = timestamps.parse(item.get("finished"))
        # The stamp holds whole seconds, and the sender of this Stop waits a second before it sends.
        # So a stamp of the second the poller started in is an earlier run's
        if at is not None and at.timestamp() > int(start) and type(item.get("queued")) is int and item["queued"] == 0:
            return True
    return False


def checks_path(root: Path, session_id: str) -> Path:
    return root / "state" / f"{session_id}.checks.jsonl"


def checks(root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Returns the outcomes recorded for the session, oldest first. A line cut short by a crash is skipped."""
    try:
        text = checks_path(root, session_id).read_text(encoding="utf-8")
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
        written = target.read_bytes() if target.exists() else b""
        # A write cut short by a crash leaves no newline, which would swallow this line too
        start = "" if not written or written.endswith(b"\n") else "\n"
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
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


def verdict_path(root: Path, session_id: str, trace_uuid: str, revision: int) -> Path:
    return root / "verdicts" / f"{session_id}.{trace_uuid}.{revision}.json"


def verdict_folder(root: Path) -> Optional[Path]:
    """Returns the folder of the saved verdicts, or None when a link stands in its place: nothing is written or removed through one."""
    folder = root / "verdicts"
    return None if folder.is_symlink() else folder


def save_verdict(root: Path, verdict: Dict[str, Any]) -> None:
    """Keeps a flagged verdict for the next prompt, in case the wake does not reach Claude. Never raises."""
    target = verdict_path(root, verdict["session_id"], verdict["trace_uuid"], verdict["revision"])
    try:
        if verdict_folder(root) is None:
            storage.log(root, "verdict not saved, its folder is a link")
            return
        target.parent.mkdir(mode=0o700, exist_ok=True)
        storage.write_json(target, verdict)
    except OSError as error:
        storage.log(root, f"verdict not saved {type(error).__name__}")


def expired(verdict: Dict[str, Any]) -> bool:
    """Says whether the verdict names a time more than seven days ago. One that names no time is not expired."""
    at = timestamps.parse(verdict.get("at"))
    return at is not None and pause.clock() - at.timestamp() > VERDICT_DAYS * 24 * 3600


def is_uuid(value: Any) -> bool:
    """Says whether the value is a UUID as the plugin writes one."""
    return isinstance(value, str) and UUID_FORM.fullmatch(value) is not None


def shaped(verdict: Dict[str, Any]) -> bool:
    """Says whether the verdict has every key settle writes, each of the type and in the form settle writes it. Its time is judged apart."""
    return (
        bool(pending.plain(verdict.get("session_id")))
        and is_uuid(verdict.get("trace_uuid"))
        and all(verdict.get(key) == "" or is_uuid(verdict.get(key)) for key in ("report_uuid", "conversation_uuid"))
        and report.is_count(verdict.get("revision"))
        and isinstance(verdict.get("endpoint"), str)
        and endpoint_problem(verdict["endpoint"]) is None
        and type(verdict.get("handed")) is bool
        and isinstance(verdict.get("text"), str)
    )


def verdict_problem(name: str, verdict: Any) -> str:
    """Returns the word for why what a verdict file holds must not be shown, or "" when all of it checks out. name is the file's name."""
    if not isinstance(verdict, dict):
        return NOT_READ
    if not storage.known_version(verdict, VERDICT_VERSION):
        return BAD_VERSION
    # Rendered with other fixed words than this code writes
    wording = verdict.get("prompt_version")
    if type(wording) is not int or wording != report.PROMPT_VERSION:
        return OTHER_WORDING
    if not shaped(verdict):
        return BAD_SHAPE
    # A file copied or renamed to another session, trace or revision
    if name != verdict_path(Path(), verdict["session_id"], verdict["trace_uuid"], verdict["revision"]).name:
        return OTHER_NAME
    # The text must be one the renderer could have written, with the link these ids give. Nothing else may stand in it
    url = report.link(verdict["endpoint"], verdict["session_id"], verdict["conversation_uuid"])
    if len(verdict["text"]) > report.TOTAL_LIMIT or not report.is_missed_message(verdict["text"], url):
        return BAD_TEXT
    at = timestamps.parse(verdict.get("at"))
    # Also a verdict that names no time
    if at is None or not -CLOCK_SLACK_S <= pause.clock() - at.timestamp() <= VERDICT_DAYS * 24 * 3600:
        return BAD_AGE
    return ""


def verdict_bytes(path: Path) -> Tuple[str, bytes]:
    """Returns the word for why a verdict file is not read, or "", and what it holds. Never raises.

    The file is opened once, and the file that was judged is the one that is read: a plain file with one name, no longer than a verdict.
    """
    # Windows has no flag that refuses a link at the open. There this look is the guard, and a file swapped after it is followed
    if not storage.NOFOLLOW and path.is_symlink():
        return A_LINK, b""
    try:
        fd = os.open(str(path), storage.READ_FLAGS)
    except (OSError, ValueError):
        return (A_LINK if path.is_symlink() else NOT_READ), b""
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode):
            return NOT_READ, b""
        # A second name is a hard link to a file from somewhere else
        if found.st_nlink != 1:
            return A_LINK, b""
        if found.st_size > VERDICT_MAX_BYTES:
            return TOO_LONG, b""
        data = b""
        # One byte past the limit: the file can grow after its size was read
        while len(data) <= VERDICT_MAX_BYTES:
            chunk = os.read(fd, VERDICT_MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return NOT_READ, b""
    finally:
        os.close(fd)
    return (TOO_LONG if len(data) > VERDICT_MAX_BYTES else ""), data


def judged(root: Path, name: str) -> Tuple[str, Any]:
    """Returns the word for why the file of that name is no verdict to show, or "", and what the file holds once it was read."""
    folder = verdict_folder(root)
    if folder is None:
        return A_LINK, None
    # The name as given, so that no part of it can lead out of the folder
    if not VERDICT_NAME.fullmatch(name):
        return BAD_NAME, None
    problem, data = verdict_bytes(folder / name)
    if problem:
        return problem, None
    try:
        verdict = storage.loads(data)
    except (ValueError, RecursionError):
        return NOT_READ, None
    return verdict_problem(name, verdict), verdict


def read_verdict(root: Path, name: str) -> Optional[Dict[str, Any]]:
    """Returns the saved verdict in the file of that name, and only when the whole file checks out. Never raises.

    Its text goes to Claude, and any process of the user can write the file. So a file is a verdict only when it is
    a plain file under a verdict's name, of a version and a wording this code writes, with every key in the form
    settle writes it, for the session, trace and revision its name gives, with a text equal to one the renderer
    writes for the data block it holds and the link its ids give, and saved within the last seven days.
    The log names why one is not shown by a fixed word, and nothing of the file.
    """
    try:
        problem, verdict = judged(root, name)
    except Exception as error:  # noqa: BLE001 - a file that cannot be judged is no verdict
        problem, verdict = f"error {type(error).__name__}", None
    if problem:
        storage.log(root, f"verdict not shown, {problem}")
        return None
    return verdict if isinstance(verdict, dict) else None


def verdicts(root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Returns the session's saved verdicts that read_verdict lets through, in file name order."""
    folder = verdict_folder(root)
    if folder is None or not pending.SESSION_ID.fullmatch(session_id):
        return []
    found = [read_verdict(root, path.name) for path in sorted(folder.glob(f"{session_id}.*.json"))]
    return [verdict for verdict in found if verdict is not None]


def prune_verdicts(root: Path) -> None:
    """Removes the verdicts of every session that are too old to show, so no report text rests on disk past its use.

    Only a file with a verdict's name that can be read, of a version this code knows, and names a time:
    anything else is not this module's to remove.
    """
    folder = verdict_folder(root)
    if folder is None:
        return
    for path in folder.glob("*.json"):
        if not VERDICT_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        verdict = storage.read_json(path, None)
        if isinstance(verdict, dict) and storage.known_version(verdict, VERDICT_VERSION) and expired(verdict):
            with contextlib.suppress(OSError):
                path.unlink()
    # A write cut short can leave report text under a name the loop above never matches
    for path in folder.glob(".*.tmp"):
        with contextlib.suppress(OSError):
            if VERDICT_TEMP_NAME.fullmatch(path.name) and not path.is_symlink() and pause.clock() - path.stat().st_mtime > STALE_TEMP_S:
                path.unlink()


def report_ids(document: Dict[str, Any]) -> Tuple[str, int]:
    """Returns Emet's uuid and revision of the report, or "" and 0 where the document names none."""
    found = document.get("report")
    named = found if isinstance(found, dict) else {}
    try:
        report_uuid = str(uuid.UUID(named.get("uuid")))
    except (ValueError, TypeError, AttributeError):
        report_uuid = ""
    revision = named.get("revision")
    return report_uuid, revision if isinstance(revision, int) and report.is_count(revision) else 0


def settle(root: Path, config: Config, stop: Stop, trace: Trace, document: Any) -> str:
    """Records a report that is ready. Returns the text to wake Claude with, or "" when Claude is not woken.

    stop is the session's latest Stop. Text is returned only for a flagged report not yet recorded,
    of a turn that carried no earlier verdict, under the cap, of the turn that Stop ended, with no newer turn under way, ready within LATE_S
    of the end of that turn, and after it was recorded as handed over.
    """
    session_id = stop.session_id
    status = report.status(document)
    # Also a word report.py knows and this module does not list
    if status not in REPORT_STATUSES:
        record(root, session_id, trace.trace_uuid, UNREADABLE)
        return ""
    report_uuid, revision = report_ids(document)
    ids = {"report_uuid": report_uuid, "revision": revision}
    if status != FLAGGED:
        record(root, session_id, trace.trace_uuid, status, ids)
        return ""
    found = checks(root, session_id)
    if known(found, trace.trace_uuid, report_uuid, revision):
        return ""
    held = hold_reason(root, config, stop, trace, found)
    link = (config.endpoint, session_id, trace.conversation_uuid)
    verdict = {
        storage.VERSION_KEY: VERDICT_VERSION,
        "session_id": session_id,
        "trace_uuid": trace.trace_uuid,
        **ids,
        "conversation_uuid": trace.conversation_uuid,
        "endpoint": config.endpoint,
        "at": storage.now(pause.clock()),
        # Which wording of the fixed words the text was rendered with
        "prompt_version": report.PROMPT_VERSION,
        # True only once the hand-over is recorded. Even then it is not proof that Claude told the user
        "handed": False,
        # The rendered text only, never the document
        "text": report.missed_message(document, *link),
    }
    # A verdict Claude is never told of is not kept: no report text rests on disk without a use
    if held != SHOWN_TURN:
        save_verdict(root, verdict)
    # Recorded before Claude is woken, so a crash in between cannot wake it twice
    line = {"handed": not held, **({"held": held} if held else {}), **ids, "prompt_version": report.PROMPT_VERSION}
    if not record(root, session_id, trace.trace_uuid, FLAGGED, line):
        return ""
    if held:
        storage.log(root, f"report {trace.trace_uuid} not handed to Claude, {held}")
        return ""
    # After the record: a crash before this line leaves a verdict that claims no hand-over
    save_verdict(root, {**verdict, "handed": True})
    return report.wake_message(document, *link)


def close(root: Path, config: Config, session_id: str, outcome: str, only: Optional[Set[str]] = None) -> str:
    """Ends a run early: every turn still waiting is recorded as not checked, and stays to be polled after a later Stop.

    With only, just those turns are recorded.
    """
    for trace in unchecked(root, config, session_id, outcome):
        if only is None or trace.trace_uuid in only:
            record(root, session_id, trace.trace_uuid, outcome)
    return ""


def unchecked(root: Path, config: Config, session_id: str, outcome: str) -> List[Trace]:
    """Returns the delivered turns with no final outcome, less than a day old, whose last outcome is not this one already."""
    found = checks(root, session_id)
    done = settled(found)
    last = {item["trace_uuid"]: item.get("outcome") for item in found if isinstance(item.get("trace_uuid"), str)}
    now = pause.clock()
    return [
        trace
        for trace in delivered(pending.lines(root, session_id), config.endpoint)
        if trace.trace_uuid not in done and now - trace.at <= MAX_AGE_S and last.get(trace.trace_uuid) != outcome
    ]


def skip(root: Path, config: Config, session_id: str, outcome: str) -> str:
    """Records why this Stop was not polled, for the newest turns already delivered. Reads each file once and never waits.

    The turn that just ended is rarely among them: the sender names it a moment later.
    """
    for trace in unchecked(root, config, session_id, outcome)[-MAX_WATCHED:]:
        record(root, session_id, trace.trace_uuid, outcome, wait=False)
    return ""


def follow(root: Path, config: Config, stop: Stop) -> str:
    """Polls the session's delivered turns until none is waiting or the time is up. Returns the text to wake Claude with, or ""."""
    session_id = stop.session_id
    # The time is counted from the Stop, also for a run that waited to take over
    start = stop.at
    idle_since = pause.clock()
    plans: Dict[str, Plan] = {}
    # Handled in this run, whether or not the outcome could be written
    done: Set[str] = set()
    # Asked for at least once in this run
    asked: Set[str] = set()
    while True:
        now = pause.clock()
        if now - start >= BUDGET_S:
            # A turn this run never asked about did not time out
            return close(root, config, session_id, TIMED_OUT, asked)
        # Emet cannot be reached, or it limited the rate of the report reads
        if pause.until(root) or pause.until(root, pause.READS):
            return close(root, config, session_id, PAUSED)
        # Read again at every step: the sender, or another Stop of this session, adds ids while this runs
        lines = pending.lines(root, session_id)
        done |= settled(checks(root, session_id))
        # Recorded, so a turn that is never polled does not look like one that was missed
        commands = own_command_turns(lines)
        for trace in delivered(lines, config.endpoint, by_plugin=True):
            if trace.trace_uuid not in done:
                done.add(trace.trace_uuid)
                record(root, session_id, trace.trace_uuid, OWN_COMMAND if trace.trace_uuid in commands else WAKE_TURN)
        waiting = []
        for trace in delivered(lines, config.endpoint):
            if trace.trace_uuid in done:
                continue
            if now - trace.at > MAX_AGE_S:
                done.add(trace.trace_uuid)
                record(root, session_id, trace.trace_uuid, EXPIRED)
                continue
            waiting.append(trace)
        watched = waiting[-MAX_WATCHED:]
        if watched:
            idle_since = now
        elif sender_done(lines, start) or now - idle_since >= IDLE_S:
            return ""
        for trace in watched:
            plan = plans.setdefault(trace.trace_uuid, Plan(max(now, start + FIRST_S)))
            if plan.due > now:
                continue
            # Checked before each request: the ones before it in this pass can each take the full timeout
            if pause.clock() - start >= BUDGET_S:
                break
            asked.add(trace.trace_uuid)
            answer = read_report(config, trace.trace_uuid)
            if answer.status in (200, 202, 404):
                # Emet serves the reads again, so the next limit starts from the shortest pause
                pause.clear(root, pause.READS)
            if answer.status == 200:
                done.add(trace.trace_uuid)
                # Read again here: a later Stop of the session can have left while this one ran
                text = settle(root, config, latest(root, stop), trace, answer.document)
                if text:
                    return text
            elif answer.status == 404 and answer.code in FINAL_MISSES:
                done.add(trace.trace_uuid)
                record(root, session_id, trace.trace_uuid, answer.code)
            elif answer.status in (401, 403):
                return close(root, config, session_id, KEY_REFUSED)
            elif answer.status == 429:
                # The reads alone: a limit on them must not hold the turns that wait to be sent
                pause.extend(root, answer.retry_after, pause.READS)
                show(root)
                return close(root, config, session_id, RATE_LIMITED)
            else:
                if answer.status != 202:
                    storage.log(root, f"report {trace.trace_uuid} -> {answer.status or 'network'}, asked again later")
                wait = max(float(STEPS_S[min(plan.step, len(STEPS_S) - 1)]), answer.retry_after or 0.0)
                plan.step += 1
                soonest = pause.clock() + max(float(STEPS_S[0]), answer.retry_after or 0.0)
                plan.due = before_the_window_closes(pause.clock() + wait * (1 + extra()), turn_end(trace, stop), soonest)
        now = pause.clock()
        due = [plans[trace.trace_uuid].due for trace in watched if trace.trace_uuid not in done]
        until = min([*due, now + TICK_S, start + BUDGET_S])
        sleep(max(until - now, SHORTEST_SLEEP_S))


def attend(root: Path, config: Config, stop: Stop) -> str:
    """Polls as the session's one watcher, at once or after the one already running has left. Never waits on the lock itself.

    Only the latest Stop of the session waits to take over. Its own time still ends the wait.
    """
    waited = False
    while True:
        with storage.try_locked(root / "state" / f"{stop.session_id}.watch.lock") as held:
            if held:
                prune_verdicts(root)
                return follow(root, config, stop)
        if not waited:
            storage.log(root, "watch waits, the session already has a watcher")
            waited = True
        if latest(root, stop).token != stop.token:
            storage.log(root, "watch left, a later Stop of the session waits")
            return ""
        if pause.clock() - stop.at + TICK_S >= BUDGET_S:
            storage.log(root, "watch left, the watcher before it ran on")
            return ""
        sleep(TICK_S)


def poll(raw: bytes, environ: Mapping[str, str]) -> str:
    """Decides whether this Stop is polled at all, and polls. Returns the text to wake Claude with, or ""."""
    began = pause.clock()
    hook_input = storage.loads(raw or b"{}")
    if not isinstance(hook_input, dict):
        return ""
    config = load(environ)
    session_id = str(hook_input.get("session_id") or "")
    # The session id names files
    if not pending.SESSION_ID.fullmatch(session_id):
        return ""
    root = storage.data_dir(environ)
    # The turn after a wake is sent and checked too. Polling at its Stop would let one wake follow another
    if started_by_hook(hook_input):
        storage.log(root, "watch skipped, a hook started the turn")
        return ""
    if config.problem:
        storage.log(root, "watch skipped, the plugin is not set up")
        return skip(root, config, session_id, NOT_CONFIGURED)
    transcript_path = str(hook_input.get("transcript_path") or "")
    found, size = session_entries(transcript_path)
    # Before any wait: an older Claude Code would run this hook in the foreground
    if claude_code_version(found) < MIN_CLAUDE_CODE:
        storage.log(root, "watch skipped, Claude Code is older than 2.1.287 or its version is not known")
        return skip(root, config, session_id, OLD_CLAUDE_CODE)
    if headless(found, environ):
        storage.log(root, "watch skipped, no person is at the session")
        return skip(root, config, session_id, NO_PERSON)
    stop = Stop(session_id, transcript_path, pending.plain(hook_input.get("prompt_id")), size, began, uuid.uuid4().hex, last_time(found))
    # Written before the lock is tried: the watcher already running judges this turn by it
    announce(root, stop)
    return attend(root, config, stop)


def run(raw: bytes, environ: Mapping[str, str] = os.environ) -> int:
    """Runs the poller for one Stop. Returns 0, except after a flagged report was handed to Claude."""
    timer = hard_stop()
    try:
        text = poll(raw, environ)
        if text:
            return wake(text)
    except Exception as error:  # noqa: BLE001 - the poller must never break the session
        with contextlib.suppress(Exception):
            storage.log(storage.data_dir(environ), f"watch error {type(error).__name__}")
    finally:
        timer.cancel()
    return 0
