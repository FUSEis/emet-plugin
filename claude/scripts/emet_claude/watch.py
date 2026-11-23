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
"""

from __future__ import annotations

import contextlib
import http.client
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from . import VERSION, checks, pause, pending, report, sessions, storage, timestamps, transport, verdicts
from .config import Config, load

# The first Claude Code that reports a waking hook whose script is missing once, not at every turn. The README requires it
MIN_CLAUDE_CODE = (2, 1, 287)


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


# Turns polled at once, newest first, so a long backlog cannot become a burst of requests
MAX_WATCHED = 5
# A turn delivered longer ago than this is no longer polled
MAX_AGE_S = 24 * 3600


# The format of the Stop file. A file that names no version is read as 1
STOP_VERSION = 1


WAKE_EXIT = 2


@dataclass(frozen=True)
class Answer:
    """What Emet answered a report read with. status is 0 when it could not be reached."""

    status: int
    code: str = ""
    document: Any = None
    retry_after: Optional[float] = None


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
    # The steady clock when the run started. The waits of the run are counted from it. Never saved
    steady: float = 0.0


@dataclass
class Plan:
    """When a turn's report is asked for next, on the steady clock, and how many times it was not ready."""

    due: float
    step: int = 0
    # The earliest the next read may start, on the steady clock
    soonest: float = 0.0


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def extra() -> float:
    """Returns the share added to a wait. Never negative: a wait is only ever made longer."""
    return random.uniform(0, EXTRA)  # noqa: S311 - timing, not a secret


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


def newer_turn(stop: Stop) -> bool:
    """Says whether the session file shows a turn started after the one this Stop ended."""
    found, _ = sessions.session_entries(stop.transcript_path, stop.size)
    starts = sessions.turn_starts(found)
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
        saved["ended"] = storage.now(stop.ended)
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
    # Cut to whole seconds: the stamp of a later Stop is cut too, to seconds by an older plugin, and must not read as earlier
    if at is None or at.timestamp() < int(own.at) or not isinstance(token, str) or token in ("", own.token):
        return own
    # The size tells an earlier Stop of the same second from a later one
    if type(size) is not int or size < own.size:
        return own
    ended = timestamps.parse(saved.get("ended"))
    return Stop(
        own.session_id,
        own.transcript_path,
        pending.plain(prompt_id),
        size,
        at.timestamp(),
        token,
        ended.timestamp() if ended else 0.0,
        own.steady,
    )


def earlier_turn(trace: sessions.Trace, stop: Stop) -> bool:
    """Says whether the trace is of a turn before the one the Stop ended."""
    if trace.prompt_id and stop.prompt_id:
        return trace.prompt_id != stop.prompt_id
    # Without both ids: a turn is delivered after it ends, so one delivered before the last entry of the latest turn is an earlier one
    # Cut to whole seconds: an older plugin stamps a line so, and its line of the latest turn must not read as earlier
    return trace.at < int(stop.ended)


def turn_end(trace: sessions.Trace, stop: Stop) -> float:
    """Returns when the trace's turn ended: by its own line, else the last entry the Stop read, else the second the Stop began in."""
    # Cut to whole seconds: the run's own Stop and one read from a file an older plugin stamped give the same end
    return trace.ended or stop.ended or float(int(stop.at))


def hold_reason(root: Path, config: Config, stop: Stop, trace: sessions.Trace, found: List[Dict[str, Any]]) -> str:
    """Returns why a flagged report is not handed to Claude, or "" when it is. stop is the session's latest Stop.

    The first reason that applies is the one recorded: a turn that carried a verdict, a scheduled turn, the cap,
    then a newer turn, then a late report.
    """
    # Its answer quotes the claims of the verdict it carried. Telling Claude of that would start a chain
    if trace.prompt_id and trace.prompt_id in checks.shown_prompts(found):
        return checks.SHOWN_TURN
    # No one reads along, and a wake would spend a turn. Before the rest, so the word always names such a turn: showings count by it
    if trace.scheduled:
        return checks.SCHEDULED_TURN
    if checks.wakes(found) >= WAKE_CAP:
        return checks.CAPPED
    # A wake that lands in a running turn is folded into it and can go unseen
    if earlier_turn(trace, stop) or sessions.newer_trace(root, config.endpoint, stop.session_id, trace) or newer_turn(stop):
        return checks.NEWER_TURN
    # The user has moved on by then. Asked last: only the report of the turn in view can be called late
    if pause.clock() - turn_end(trace, stop) > LATE_S:
        return checks.LATE
    return ""


def on_the_steady_clock(at: float) -> float:
    """Returns what the steady clock reads at a time of the system clock, as the two stand now."""
    return pause.steady() + at - pause.clock()


def before_the_window_closes(due: float, end: float, soonest: float) -> float:
    """Returns when to ask next: as planned, or sooner, so that one read still starts inside the time a wake is allowed in.

    The waits only grow by chance and by the time requests take, so the planned read would often come just after it.
    Never sooner than soonest: the shortest wait, or the one Emet named. due and soonest are on the steady clock.
    end is the time of the system clock the turn ended at, as the late rule reads it.
    """
    edge = on_the_steady_clock(end + LATE_S - EDGE_S)
    return edge if soonest <= edge < due else due


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


def settle(root: Path, config: Config, stop: Stop, trace: sessions.Trace, document: Any) -> str:
    """Records a report that is ready. Returns the text to wake Claude with, or "" when Claude is not woken.

    stop is the session's latest Stop. Text is returned only for a flagged report not yet recorded,
    of a turn that carried no earlier verdict and was not started on a schedule, under the cap, of the turn that Stop ended,
    with no newer turn under way, ready within LATE_S of the end of that turn, and after it was recorded as handed over.
    """
    session_id = stop.session_id
    status = report.status(document)
    # Also a word report.py knows and this module does not list
    if status not in checks.REPORT_STATUSES:
        checks.record(root, session_id, trace.trace_uuid, checks.UNREADABLE)
        return ""
    report_uuid, revision = report_ids(document)
    ids = {"report_uuid": report_uuid, "revision": revision}
    if status != checks.FLAGGED:
        checks.record(root, session_id, trace.trace_uuid, status, ids)
        return ""
    found = checks.checks(root, session_id)
    if checks.known(found, trace.trace_uuid, report_uuid, revision):
        return ""
    held = hold_reason(root, config, stop, trace, found)
    link = (config.endpoint, session_id, trace.conversation_uuid)
    verdict = {
        storage.VERSION_KEY: verdicts.VERDICT_VERSION,
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
    if held != checks.SHOWN_TURN:
        verdicts.save_verdict(root, verdict)
    # Recorded before Claude is woken, so a crash in between cannot wake it twice
    line = {"handed": not held, **({"held": held} if held else {}), **ids, "prompt_version": report.PROMPT_VERSION}
    if not checks.record(root, session_id, trace.trace_uuid, checks.FLAGGED, line):
        return ""
    if held:
        storage.log(root, f"report {trace.trace_uuid} not handed to Claude, {held}")
        return ""
    # After the record: a crash before this line leaves a verdict that claims no hand-over
    verdicts.save_verdict(root, {**verdict, "handed": True})
    return report.wake_message(document, *link)


def close(root: Path, config: Config, session_id: str, outcome: str, only: Optional[Set[str]] = None) -> str:
    """Ends a run early: every turn still waiting is recorded as not checked, and stays to be polled after a later Stop.

    With only, just those turns are recorded.
    """
    for trace in unchecked(root, config, session_id, outcome):
        if only is None or trace.trace_uuid in only:
            checks.record(root, session_id, trace.trace_uuid, outcome)
    return ""


def unchecked(root: Path, config: Config, session_id: str, outcome: str) -> List[sessions.Trace]:
    """Returns the delivered turns with no final outcome, less than a day old, whose last outcome is not this one already."""
    found = checks.checks(root, session_id)
    done = checks.settled(found)
    last = {item["trace_uuid"]: item.get("outcome") for item in found if isinstance(item.get("trace_uuid"), str)}
    now = pause.clock()
    return [
        trace
        for trace in sessions.delivered(pending.lines(root, session_id), config.endpoint)
        if trace.trace_uuid not in done and now - trace.at <= MAX_AGE_S and last.get(trace.trace_uuid) != outcome
    ]


def skip(root: Path, config: Config, session_id: str, outcome: str) -> str:
    """Records why this Stop was not polled, for the newest turns already delivered. Reads each file once and never waits.

    The turn that just ended is rarely among them: the sender names it a moment later.
    """
    for trace in unchecked(root, config, session_id, outcome)[-MAX_WATCHED:]:
        checks.record(root, session_id, trace.trace_uuid, outcome, wait=False)
    return ""


def follow(root: Path, config: Config, stop: Stop) -> str:
    """Polls the session's delivered turns until none is waiting or the time is up. Returns the text to wake Claude with, or ""."""
    session_id = stop.session_id
    # The time is counted from the Stop, also for a run that waited to take over
    start = stop.steady
    idle_since = pause.steady()
    plans: Dict[str, Plan] = {}
    # Handled in this run, whether or not the outcome could be written
    done: Set[str] = set()
    # Asked for at least once in this run
    asked: Set[str] = set()
    while True:
        now = pause.steady()
        if now - start >= BUDGET_S:
            # A turn this run never asked about did not time out
            return close(root, config, session_id, checks.TIMED_OUT, asked)
        # Emet cannot be reached, or it limited the rate of the report reads
        if pause.until(root) or pause.until(root, pause.READS):
            return close(root, config, session_id, checks.PAUSED)
        # Read again at every step: the sender, or another Stop of this session, adds ids while this runs
        lines = pending.lines(root, session_id)
        done |= checks.settled(checks.checks(root, session_id))
        # Recorded, so a turn that is never polled does not look like one that was missed
        commands = sessions.own_command_turns(lines)
        for trace in sessions.delivered(lines, config.endpoint, by_plugin=True):
            if trace.trace_uuid not in done:
                done.add(trace.trace_uuid)
                checks.record(root, session_id, trace.trace_uuid, checks.OWN_COMMAND if trace.trace_uuid in commands else checks.WAKE_TURN)
        waiting = []
        for trace in sessions.delivered(lines, config.endpoint):
            if trace.trace_uuid in done:
                continue
            # The system clock: the line was stamped by another process
            if pause.clock() - trace.at > MAX_AGE_S:
                done.add(trace.trace_uuid)
                checks.record(root, session_id, trace.trace_uuid, checks.EXPIRED)
                continue
            waiting.append(trace)
        watched = waiting[-MAX_WATCHED:]
        if watched:
            idle_since = now
        elif sessions.sender_done(lines, stop.at) or now - idle_since >= IDLE_S:
            return ""
        for trace in watched:
            first = max(now, start + FIRST_S)
            plan = plans.setdefault(trace.trace_uuid, Plan(first, soonest=first))
            # A change of the system clock moves the edge of the window after the read was planned
            plan.due = before_the_window_closes(plan.due, turn_end(trace, stop), plan.soonest)
            if plan.due > now:
                continue
            # Checked before each request: the ones before it in this pass can each take the full timeout
            if pause.steady() - start >= BUDGET_S:
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
            elif answer.status == 404 and answer.code in checks.FINAL_MISSES:
                done.add(trace.trace_uuid)
                checks.record(root, session_id, trace.trace_uuid, answer.code)
            elif answer.status in (401, 403):
                return close(root, config, session_id, checks.KEY_REFUSED)
            elif answer.status == 429:
                # The reads alone: a limit on them must not hold the turns that wait to be sent
                pause.extend(root, answer.retry_after, pause.READS)
                checks.show(root)
                return close(root, config, session_id, checks.RATE_LIMITED)
            else:
                if answer.status != 202:
                    storage.log(root, f"report {trace.trace_uuid} -> {answer.status or 'network'}, asked again later")
                wait = max(float(STEPS_S[min(plan.step, len(STEPS_S) - 1)]), answer.retry_after or 0.0)
                plan.step += 1
                plan.soonest = pause.steady() + max(float(STEPS_S[0]), answer.retry_after or 0.0)
                plan.due = before_the_window_closes(pause.steady() + wait * (1 + extra()), turn_end(trace, stop), plan.soonest)
        now = pause.steady()
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
                verdicts.prune_verdicts(root)
                return follow(root, config, stop)
        if not waited:
            storage.log(root, "watch waits, the session already has a watcher")
            waited = True
        if latest(root, stop).token != stop.token:
            storage.log(root, "watch left, a later Stop of the session waits")
            return ""
        if pause.steady() - stop.steady + TICK_S >= BUDGET_S:
            storage.log(root, "watch left, the watcher before it ran on")
            return ""
        sleep(TICK_S)


def poll(raw: bytes, environ: Mapping[str, str]) -> str:
    """Decides whether this Stop is polled at all, and polls. Returns the text to wake Claude with, or ""."""
    began, steady = pause.clock(), pause.steady()
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
    if sessions.started_by_hook(hook_input):
        storage.log(root, "watch skipped, a hook started the turn")
        return ""
    if config.problem:
        storage.log(root, "watch skipped, the plugin is not set up")
        return skip(root, config, session_id, checks.NOT_CONFIGURED)
    transcript_path = str(hook_input.get("transcript_path") or "")
    found, size = sessions.session_entries(transcript_path)
    # Before any wait: an older Claude Code would run this hook in the foreground
    if sessions.claude_code_version(found) < MIN_CLAUDE_CODE:
        storage.log(root, "watch skipped, Claude Code is older than 2.1.287 or its version is not known")
        return skip(root, config, session_id, checks.OLD_CLAUDE_CODE)
    if sessions.headless(found, environ):
        storage.log(root, "watch skipped, no person is at the session")
        return skip(root, config, session_id, checks.NO_PERSON)
    stop = Stop(
        session_id,
        transcript_path,
        pending.plain(hook_input.get("prompt_id")),
        size,
        began,
        uuid.uuid4().hex,
        sessions.last_time(found),
        steady,
    )
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
