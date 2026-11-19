"""Reads the Emet report of each delivered turn after the turn ends, and wakes Claude when a report is flagged.

Runs as its own background hook, started at every Stop, one at a time per session. It exits 0
whatever happens, except when it hands a flagged report to Claude. Each outcome is added as a line
to state/<session_id>.checks.jsonl and shown in the status file. A flagged verdict is saved to its
own file under verdicts/ before Claude is woken. Only that file holds text from a report.

Exit code 2 hands the text to Claude Code. It does not prove Claude told the user: a wake that
lands inside a running turn can go unseen. So the verdict file is kept either way.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from . import VERSION, pause, pending, report, storage, timestamps, transcript, transport
from .config import Config, load

# The first Claude Code that can run a background hook which wakes Claude
MIN_CLAUDE_CODE = (2, 1, 287)
VERSION_FORM = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}")
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
# How long the poller waits for the sender with nothing to poll
IDLE_S = 60
# How often the pending file is read again while waiting
TICK_S = 2.0
SHORTEST_SLEEP_S = 0.05
REQUEST_TIMEOUT_S = 10
REPORT_MAX_BYTES = 2 * 1024 * 1024
# The largest share added to a wait by chance
EXTRA = 0.1
# Wakes per session; later flagged reports are saved and recorded only
WAKE_CAP = 5
# Why a flagged report was not handed to Claude
CAPPED = "wake_cap"
NEWER_TURN = "newer_turn"
# Turns polled at once, newest first, so a long backlog cannot become a burst of requests
MAX_WATCHED = 5
# A turn delivered longer ago than this is no longer polled
MAX_AGE_S = 24 * 3600
VERDICT_DAYS = 7
# Checks listed in the status file
RECENT_CHECKS = 5
WAKE_EXIT = 2

FLAGGED = report.FLAGGED
UNREADABLE = "unreadable"
TIMED_OUT = "timed_out"
KEY_REFUSED = "key_refused"
RATE_LIMITED = "rate_limited"
PAUSED = "paused"
# Why a Stop was not polled at all
NOT_CONFIGURED = "not_configured"
OLD_CLAUDE_CODE = "old_claude_code"
NO_PERSON = "no_person"
# Emet's 404 codes that end the wait for a report
FINAL_MISSES = ("not_assessed", "failed", "not_found")
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


@dataclass(frozen=True)
class Stop:
    """The Stop this run follows."""

    session_id: str
    transcript_path: str
    # Claude Code's id of the turn that ended, "" when the hook input names none
    prompt_id: str
    # The size of the session file when the run started
    size: int


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


def headless(found: List[Tuple[int, Dict[str, Any]]]) -> bool:
    """Says whether no person is at the session (claude -p, the SDK). There a wake is ignored and this hook holds up the exit."""
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
            entry = json.loads(raw)
            if isinstance(entry, dict):
                found.append((position, entry))
        position += len(raw) + 1
    return found, size


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
    starts = [
        (offset, entry)
        for offset, entry in found
        if transcript.starts_turn(entry) and not entry.get("isSidechain") and not transcript.LOCAL_COMMAND.match(transcript.text_of(entry))
    ]
    if any(offset >= stop.size for offset, _ in starts):
        return True
    # A prompt queued during the turn can be in the file before this process starts. Told apart by the turn's id
    ids = [entry.get("promptId") for _, entry in starts]
    return bool(stop.prompt_id) and stop.prompt_id in ids and ids[-1] != stop.prompt_id


def newer_trace(root: Path, endpoint: str, session_id: str, trace_uuid: str) -> bool:
    """Says whether a later turn of the session has been delivered since."""
    found = delivered(pending.lines(root, session_id), endpoint)
    return bool(found) and found[-1].trace_uuid != trace_uuid


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
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


def delivered(lines: List[Dict[str, Any]], endpoint: str) -> List[Trace]:
    """Returns the turns delivered to this endpoint, once each, oldest first. Any other line is not a trace to poll."""
    found: Dict[str, Trace] = {}
    for item in lines:
        if any(key in item for key in NOT_A_TRACE) or item.get("endpoint") != endpoint:
            continue
        at = timestamps.parse(item.get("at"))
        if at is None:
            continue
        # The id goes into a request path
        try:
            trace_uuid = str(uuid.UUID(item.get("trace_uuid")))
        except (ValueError, TypeError, AttributeError):
            continue
        conversation = item.get("conversation_uuid")
        found.setdefault(trace_uuid, Trace(trace_uuid, at.timestamp(), conversation if isinstance(conversation, str) else ""))
    return list(found.values())


def sender_done(lines: List[Dict[str, Any]], start: float) -> bool:
    """Says whether a run of the sender ended since start with an empty retry queue, so no more ids can follow.

    With envelopes still queued a later run of any session can add an id, so the poller keeps waiting.
    """
    for item in lines:
        at = timestamps.parse(item.get("finished"))
        # The stamp holds whole seconds
        if at is not None and at.timestamp() >= int(start) and type(item.get("queued")) is int and item["queued"] == 0:
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
            item = json.loads(line)
            if isinstance(item, dict):
                found.append(item)
    return found


def settled(found: List[Dict[str, Any]]) -> Set[str]:
    """Returns the traces that need no more requests."""
    return {item["trace_uuid"] for item in found if item.get("final") is True and isinstance(item.get("trace_uuid"), str)}


def wakes(found: List[Dict[str, Any]]) -> int:
    """Counts the reports handed to Claude in this session."""
    return sum(1 for item in found if item.get("handed") is True)


def known(found: List[Dict[str, Any]], trace_uuid: str, report_uuid: str, revision: int) -> bool:
    """Says whether this revision of the trace's report was already recorded as flagged. Emet can check a trace again."""
    key = (trace_uuid, report_uuid, revision)
    return any(
        item.get("outcome") == FLAGGED and (item.get("trace_uuid"), item.get("report_uuid"), item.get("revision")) == key for item in found
    )


def record(
    root: Path, session_id: str, trace_uuid: str, outcome: str, final: bool, ids: Optional[Mapping[str, Any]] = None, wait: bool = True
) -> bool:
    """Adds one outcome to the session's file and the status file. Returns False when the session's file was not written.

    Without wait the status file is left alone when another process is writing it.
    """
    item = {"trace_uuid": trace_uuid, "outcome": outcome, "final": final, "at": storage.now(pause.clock()), **(ids or {})}
    target = checks_path(root, session_id)
    try:
        written = target.read_bytes() if target.exists() else b""
        # A write cut short by a crash leaves no newline, which would swallow this line too
        start = "" if not written or written.endswith(b"\n") else "\n"
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(start + json.dumps(item) + "\n")
    except OSError as error:
        storage.log(root, f"report {trace_uuid} -> {outcome}, not recorded {type(error).__name__}")
        return False
    storage.log(root, f"report {trace_uuid} -> {outcome}")
    show(root, {"session_id": session_id, **item}, wait)
    return True


def show(root: Path, check: Dict[str, Any], wait: bool = True) -> None:
    """Lists the check in the status file, newest first and once per trace. Never raises."""
    lock = root / "status.lock"
    try:
        with storage.locked(lock) if wait else storage.try_locked(lock) as held:
            if held is False:
                storage.log(root, "check not shown, the status file is in use")
                return
            status = storage.read_json(root / "status.json", {})
            if not isinstance(status, dict):
                status = {}
            saved = status.get("checks")
            earlier = saved if isinstance(saved, list) else []
            others = [item for item in earlier if isinstance(item, dict) and item.get("trace_uuid") != check["trace_uuid"]]
            status["checks"] = [check, *others][:RECENT_CHECKS]
            storage.write_json(root / "status.json", status)
    except OSError as error:
        storage.log(root, f"check not shown {type(error).__name__}")


def verdict_path(root: Path, session_id: str, trace_uuid: str, revision: int) -> Path:
    return root / "verdicts" / f"{session_id}.{trace_uuid}.{revision}.json"


def save_verdict(root: Path, verdict: Dict[str, Any]) -> None:
    """Keeps a flagged verdict for the next prompt, in case the wake does not reach Claude. Never raises."""
    target = verdict_path(root, verdict["session_id"], verdict["trace_uuid"], verdict["revision"])
    try:
        target.parent.mkdir(mode=0o700, exist_ok=True)
        storage.write_json(target, verdict)
    except OSError as error:
        storage.log(root, f"verdict not saved {type(error).__name__}")


def expired(verdict: Any) -> bool:
    at = timestamps.parse(verdict.get("at")) if isinstance(verdict, dict) else None
    return at is None or pause.clock() - at.timestamp() > VERDICT_DAYS * 24 * 3600


def verdicts(root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Returns the session's saved verdicts that are still eligible to be shown, in file name order."""
    if not pending.SESSION_ID.fullmatch(session_id):
        return []
    found = [storage.read_json(path, None) for path in sorted((root / "verdicts").glob(f"{session_id}.*.json"))]
    return [verdict for verdict in found if not expired(verdict)]


def prune_verdicts(root: Path) -> None:
    """Removes the verdicts of every session that are too old to show, so no report text rests on disk past its use."""
    for path in (root / "verdicts").glob("*.json"):
        if expired(storage.read_json(path, None)):
            with contextlib.suppress(OSError):
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

    Text is returned only for a flagged report not yet recorded, under the cap, with no newer turn
    under way, and after it was recorded as handed over.
    """
    session_id = stop.session_id
    status = report.status(document)
    if not status:
        record(root, session_id, trace.trace_uuid, UNREADABLE, True)
        return ""
    report_uuid, revision = report_ids(document)
    ids = {"report_uuid": report_uuid, "revision": revision}
    if status != FLAGGED:
        record(root, session_id, trace.trace_uuid, status, True, ids)
        return ""
    found = checks(root, session_id)
    if known(found, trace.trace_uuid, report_uuid, revision):
        return ""
    held = ""
    if wakes(found) >= WAKE_CAP:
        held = CAPPED
    # A wake that lands in a running turn is folded into it and can go unseen
    elif newer_trace(root, config.endpoint, session_id, trace.trace_uuid) or newer_turn(stop):
        held = NEWER_TURN
    link = (config.endpoint, session_id, trace.conversation_uuid)
    verdict = {
        "session_id": session_id,
        "trace_uuid": trace.trace_uuid,
        **ids,
        "conversation_uuid": trace.conversation_uuid,
        "endpoint": config.endpoint,
        "at": storage.now(pause.clock()),
        # Handed to Claude Code, which is not proof that Claude told the user
        "handed": not held,
        # The rendered text only, never the document
        "text": report.missed_message(document, *link),
    }
    save_verdict(root, verdict)
    # Recorded before Claude is woken, so a crash in between cannot wake it twice
    if not record(root, session_id, trace.trace_uuid, FLAGGED, True, {"handed": not held, **({"held": held} if held else {}), **ids}):
        return ""
    if held:
        storage.log(root, f"report {trace.trace_uuid} not handed to Claude, {held}")
        return ""
    return report.wake_message(document, *link)


def close(root: Path, config: Config, session_id: str, outcome: str) -> str:
    """Ends a run early: every turn still waiting is recorded as not checked, and stays to be polled after a later Stop."""
    for trace in unchecked(root, config, session_id, outcome):
        record(root, session_id, trace.trace_uuid, outcome, False)
    return ""


def unchecked(root: Path, config: Config, session_id: str, outcome: str) -> List[Trace]:
    """Returns the delivered turns with no final outcome, less than a day old, whose last outcome is not this one already."""
    found = checks(root, session_id)
    done = settled(found)
    last = {item.get("trace_uuid"): item.get("outcome") for item in found}
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
        record(root, session_id, trace.trace_uuid, outcome, False, wait=False)
    return ""


def follow(root: Path, config: Config, stop: Stop) -> str:
    """Polls the session's delivered turns until none is waiting or the time is up. Returns the text to wake Claude with, or ""."""
    session_id = stop.session_id
    start = pause.clock()
    idle_since = start
    plans: Dict[str, Plan] = {}
    # Handled in this run, whether or not the outcome could be written
    done: Set[str] = set()
    while True:
        now = pause.clock()
        if now - start >= BUDGET_S:
            return close(root, config, session_id, TIMED_OUT)
        if pause.until(root):
            return close(root, config, session_id, PAUSED)
        # Read again at every step: the sender, or another Stop of this session, adds ids while this runs
        lines = pending.lines(root, session_id)
        done |= settled(checks(root, session_id))
        waiting = []
        for trace in delivered(lines, config.endpoint):
            if trace.trace_uuid in done:
                continue
            if now - trace.at > MAX_AGE_S:
                done.add(trace.trace_uuid)
                record(root, session_id, trace.trace_uuid, TIMED_OUT, True)
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
            answer = read_report(config, trace.trace_uuid)
            if answer.status == 200:
                done.add(trace.trace_uuid)
                text = settle(root, config, stop, trace, answer.document)
                if text:
                    return text
            elif answer.status == 404 and answer.code in FINAL_MISSES:
                done.add(trace.trace_uuid)
                record(root, session_id, trace.trace_uuid, answer.code, True)
            elif answer.status in (401, 403):
                return close(root, config, session_id, KEY_REFUSED)
            elif answer.status == 429:
                pause.extend(root, answer.retry_after)
                return close(root, config, session_id, RATE_LIMITED)
            else:
                if answer.status != 202:
                    storage.log(root, f"report {trace.trace_uuid} -> {answer.status or 'network'}, asked again later")
                wait = max(float(STEPS_S[min(plan.step, len(STEPS_S) - 1)]), answer.retry_after or 0.0)
                plan.step += 1
                plan.due = pause.clock() + wait * (1 + extra())
        now = pause.clock()
        due = [plans[trace.trace_uuid].due for trace in watched if trace.trace_uuid not in done]
        until = min([*due, now + TICK_S, start + BUDGET_S])
        sleep(max(until - now, SHORTEST_SLEEP_S))


def poll(raw: bytes, environ: Mapping[str, str]) -> str:
    """Decides whether this Stop is polled at all, and polls. Returns the text to wake Claude with, or ""."""
    hook_input = json.loads(raw or b"{}")
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
        return skip(root, config, session_id, NOT_CONFIGURED)
    transcript_path = str(hook_input.get("transcript_path") or "")
    found, size = session_entries(transcript_path)
    # Before any wait: an older Claude Code would run this hook in the foreground
    if claude_code_version(found) < MIN_CLAUDE_CODE:
        storage.log(root, "watch skipped, Claude Code is older than 2.1.287 or its version is not known")
        return skip(root, config, session_id, OLD_CLAUDE_CODE)
    if headless(found):
        storage.log(root, "watch skipped, no person is at the session")
        return skip(root, config, session_id, NO_PERSON)
    prompt_id = hook_input.get("prompt_id")
    stop = Stop(session_id, transcript_path, prompt_id if isinstance(prompt_id, str) else "", size)
    # Claude Code starts this process at every Stop. One per session is enough, and it never waits for the other
    with storage.try_locked(root / "state" / f"{session_id}.watch.lock") as held:
        if not held:
            storage.log(root, "watch skipped, the session already has a watcher")
            return ""
        prune_verdicts(root)
        return follow(root, config, stop)


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
