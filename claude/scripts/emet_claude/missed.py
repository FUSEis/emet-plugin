"""Gives Claude a flagged verdict whose wake was held back or lost, with the next prompt the user sends.

Runs in the foreground at every prompt (UserPromptSubmit), so the session waits for it. It reads
the plugin's own files and the end of the session file, makes no request and waits for no lock:
the locks it needs are tried, and one that is not free means nothing is shown at this prompt.
It always exits 0. It prints nothing, or one JSON object that adds context for Claude and one line
for the user. It never sets a field that can block or change the prompt.

The checks file decides, and the verdict file only supplies the text. A verdict is shown when
its `flagged` line says Claude was not woken ("handed" false), or says it was, no watcher of the
session is still running, and the session file holds no wake since. It is shown once, in the session that produced it, one per prompt,
the newest first, and at most SHOW_CAP times in a session. The verdicts of turns Claude Code started on a schedule
are counted apart, up to SCHEDULED_SHOW_CAP, so a loop cannot use up the showings of a person's own turns.
The showing is recorded as `shown` before anything is printed, and the file is removed then, so a crash can lose a showing and never
repeat one.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from . import checks, pause, pending, report, sessions, storage, timestamps, transcript, verdicts

EVENT = "UserPromptSubmit"
# Showings per session, counted apart from the wakes: a report held back by the wake cap is shown here
SHOW_CAP = 3
# The same for the reports of turns Claude Code started on a schedule, counted apart from the ones above
SCHEDULED_SHOW_CAP = 3
# A wake handed over less than this long ago may not be in the session file yet, so it is not judged
HANDED_GRACE_S = 10
# What a slash command and a shell escape begin with. No model call answers most of them
NOT_A_PROMPT = ("/", "!")
# Shown to the user by Claude Code (systemMessage). Fixed words, never text from a report
USER_LINE = "Emet disputed a claim in an earlier answer of this session. Claude was given Emet's report with this prompt."

# How a wake begins, as report.render writes it, with its line break as a space
WAKE_START = f"{report.WAKE_OPENING} {report.WAKE_NOTICE}"
# What Claude Code puts first when it queues a notice, such as a wake
NOTICE_START = "<task-notification>"

Entries = List[Tuple[int, Dict[str, Any]]]


def output(text: str) -> bytes:
    """Returns what the hook prints: the verdict as context for Claude, and one line for the user. ASCII, so no pipe can change it."""
    shown = {"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": text}, "systemMessage": USER_LINE}
    return json.dumps(shown).encode("ascii")


def carries(hook_input: Mapping[str, Any]) -> bool:
    """Says whether the prompt is one a verdict can come with: text a model call answers, in the main conversation."""
    prompt = hook_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or hook_input.get("agent_id"):
        return False
    # The turn a wake starts is about a verdict already
    return not prompt.lstrip().startswith(NOT_A_PROMPT) and report.WAKE_OPENING not in " ".join(prompt[: transcript.WAKE_SCAN].split())


def strings(value: Any) -> Iterator[str]:
    """Yields every string in a JSON value. Never recurses."""
    waiting = [value]
    while waiting:
        item = waiting.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            waiting.extend(item.values())
        elif isinstance(item, list):
            waiting.extend(item)


def enqueued(entry: Dict[str, Any], after: float) -> bool:
    """Says whether the entry is Claude Code taking a wake into its queue, after seconds past the hand-over. Seen on 2.1.305.

    Written the moment the wake arrives, whatever becomes of it: a turn of its own, or folded into a running one.
    """
    content = entry.get("content")
    if entry.get("type") != "queue-operation" or entry.get("operation") != "enqueue" or not isinstance(content, str):
        return False
    # A person's prompt does not begin with the wrapper of a notice
    if not content.startswith(NOTICE_START) or after > transcript.WAKE_WINDOW_S:
        return False
    return WAKE_START in " ".join(content[: transcript.WAKE_SCAN].split())


def folded(entry: Dict[str, Any]) -> bool:
    """Says whether the entry is a wake that landed in a running turn, as a queued_command attachment. Assumed, not seen."""
    held = entry.get("attachment")
    if entry.get("type") != "attachment" or not isinstance(held, dict) or held.get("type") != "queued_command":
        return False
    # Claude Code can wrap the lines again
    return any(WAKE_START in " ".join(text.split()) for text in strings(held.get("prompt")))


def woken(entries: Entries, handed_at: float) -> Optional[bool]:
    """Says whether the session file holds the wake that was handed over at that time. None when it cannot tell.

    True when an entry written since then is the wake taken into Claude Code's queue, the turn that wake started,
    by the test the sender marks such a turn with, or the wake folded into a running turn. The sentence anywhere
    else, as in a prompt or a tool result, is no wake.
    False only when the part of the file that was read begins before the hand-over and holds none.
    """
    if pause.clock() - handed_at < HANDED_GRACE_S:
        return None
    reaches_back = False
    for _, entry in entries:
        at = timestamps.parse(entry.get("timestamp"))
        if at is None:
            continue
        if at.timestamp() < handed_at:
            reaches_back = True
        elif enqueued(entry, at.timestamp() - handed_at) or transcript.started_by_wake(entry, [handed_at]) or folded(entry):
            return True
    return False if reaches_back else None


def compacted(entries: Entries, since: float) -> bool:
    """Says whether the conversation was compacted after that time, so the answer a verdict is about is no longer in view."""
    for _, entry in entries:
        at = timestamps.parse(entry.get("timestamp"))
        if entry.get("isCompactSummary") is True and at is not None and at.timestamp() > since:
            return True
    return False


def saved_at(verdict: Dict[str, Any]) -> float:
    at = timestamps.parse(verdict.get("at"))
    return at.timestamp() if at else 0.0


def report_key(item: Mapping[str, Any]) -> Tuple[Any, Any, Any]:
    """Names one revision of one turn's report, as a verdict and a line of the checks file both hold it."""
    return item.get("trace_uuid"), item.get("report_uuid"), item.get("revision")


def recorded(found: List[Dict[str, Any]], verdict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Returns the `flagged` line of the report the verdict was saved for, or None when the poller recorded none."""
    lines = [item for item in found if item.get("outcome") == checks.FLAGGED and report_key(item) == report_key(verdict)]
    return lines[-1] if lines else None


def on_schedule(found: List[Dict[str, Any]], item: Dict[str, Any]) -> bool:
    """Says whether the report is of a turn Claude Code started on a schedule, by the reason its `flagged` line names."""
    line = recorded(found, item)
    return line is not None and line.get("held") == checks.SCHEDULED_TURN


def remove(root: Path, verdict: Dict[str, Any], why: str = "") -> None:
    """Removes a verdict's file, so no report text rests on disk past its use. Never raises."""
    try:
        verdicts.verdict_path(root, verdict["session_id"], verdict["trace_uuid"], verdict["revision"]).unlink()
    except OSError as error:
        # The record decides what is shown. A file that stays is removed at a later prompt, or once it is too old
        storage.log(root, f"verdict {verdict['trace_uuid']} not removed {type(error).__name__}")
        return
    if why:
        storage.log(root, f"verdict {verdict['trace_uuid']} removed, {why}")


def watched(root: Path, session_id: str) -> bool:
    """Says whether a watcher of the session is running. Its lock is tried without a wait and let go at once."""
    with storage.try_locked(root / "state" / f"{session_id}.watch.lock") as free:
        return not free


def missed(line: Dict[str, Any], entries: Entries, watching: bool) -> Optional[bool]:
    """Says whether Claude was never given the verdict of this `flagged` line. None when that cannot be told, and the file waits."""
    if line.get("handed") is False:
        return True
    at = timestamps.parse(line.get("at"))
    if line.get("handed") is not True or at is None:
        return None
    # The watcher that recorded the hand-over may not have written its wake yet, however long ago that was
    if watching:
        return None
    found = woken(entries, at.timestamp())
    return None if found is None else not found


def due(root: Path, session_id: str, saved: List[Dict[str, Any]], entries: Entries, watching: bool) -> List[Dict[str, Any]]:
    """Returns the session's verdicts that can be shown now, newest first, and removes the files that never will be.

    watching says whether a watcher of the session was running before entries were read.
    """
    found = checks.checks(root, session_id)
    showings = [item for item in found if item.get("outcome") == checks.SHOWN]
    shown = [report_key(item) for item in showings]
    waiting = []
    for verdict in saved:
        line = recorded(found, verdict)
        # By the report, not the turn: a later revision of a report already shown is a new report
        if report_key(verdict) in shown:
            # Left by a showing that could not remove it
            remove(root, verdict)
            continue
        if line is None:
            # Not recorded: the poller settles this report again, and may still wake Claude with it
            continue
        never_given = missed(line, entries, watching)
        if never_given is False:
            remove(root, verdict, "Claude was woken with it")
        elif compacted(entries, saved_at(verdict)):
            remove(root, verdict, "the conversation was compacted since")
        elif never_given:
            waiting.append(verdict)
    # Two counts, by the turn the report is about: the prompt does not say who sent it
    caps = (
        (False, SHOW_CAP, f"the session already had {SHOW_CAP} showings"),
        (True, SCHEDULED_SHOW_CAP, f"the session already had {SCHEDULED_SHOW_CAP} showings for scheduled turns"),
    )
    for scheduled, cap, why in caps:
        if sum(1 for item in showings if on_schedule(found, item) is scheduled) >= cap:
            for verdict in [verdict for verdict in waiting if on_schedule(found, verdict) is scheduled]:
                waiting.remove(verdict)
                remove(root, verdict, why)
    return sorted(waiting, key=lambda verdict: (saved_at(verdict), verdict["trace_uuid"]), reverse=True)


def choose(raw: bytes, environ: Mapping[str, str]) -> str:
    """Returns the text to give Claude with this prompt, or "" when there is none. The verdict is recorded and its file removed first."""
    hook_input = storage.loads(raw or b"{}")
    if not isinstance(hook_input, dict):
        return ""
    session_id = str(hook_input.get("session_id") or "")
    root = storage.data_dir(environ)
    # Nearly every prompt ends here, after one look at a folder. An id that is no plain name has no verdicts
    if not verdicts.verdicts(root, session_id):
        return ""
    if not carries(hook_input):
        storage.log(root, "verdict not shown, the prompt is a command or a notice")
        return ""
    # The poller knows the turn that carried a verdict by this id, and never tells Claude of its report
    prompt_id = pending.plain(hook_input.get("prompt_id"))
    if not prompt_id:
        storage.log(root, "verdict not shown, the prompt names no id")
        return ""
    # One prompt of a session at a time, so two that overlap cannot both take the same verdict. Never waited for
    with storage.try_locked(root / "state" / f"{session_id}.prompt.lock") as alone:
        if not alone:
            storage.log(root, "verdict not shown, another prompt of the session is being checked")
            return ""
        return taken(root, hook_input, environ, session_id, prompt_id)


def taken(root: Path, hook_input: Mapping[str, Any], environ: Mapping[str, str], session_id: str, prompt_id: str) -> str:
    """Records the verdict that comes with this prompt, removes its file and returns its text, or "". Runs under the prompt lock."""
    # Asked before the session file is read: a watcher that ends after this look can have written its wake after that read
    watching = watched(root, session_id)
    entries, _ = sessions.session_entries(str(hook_input.get("transcript_path") or ""))
    if sessions.headless(entries, environ):
        storage.log(root, "verdict not shown, no person is at the session")
        return ""
    # Read again under the lock: the prompt before this one may have just taken one
    waiting = due(root, session_id, verdicts.verdicts(root, session_id), entries, watching)
    if not waiting:
        return ""
    verdict = waiting[0]
    ids = {
        "report_uuid": verdict["report_uuid"],
        "revision": verdict["revision"],
        "prompt_version": verdict["prompt_version"],
        "prompt_id": prompt_id,
    }
    # Recorded before it is shown, so a crash in between cannot show it twice. Never waits for the status file
    if not checks.record(root, session_id, verdict["trace_uuid"], checks.SHOWN, ids, wait=False):
        return ""
    remove(root, verdict)
    return str(verdict["text"])


def run(raw: bytes, environ: Mapping[str, str] = os.environ) -> int:
    """Runs the hook for one prompt. Always returns 0: any other exit code is an error to Claude Code, and 2 would reject the prompt."""
    try:
        text = choose(raw, environ)
        if text:
            # Written whole, as bytes, and only here: Claude Code reads stdout as one JSON object
            sys.stdout.buffer.write(output(text))
            sys.stdout.buffer.flush()
    except Exception as error:  # noqa: BLE001 - the prompt must go through whatever fails here
        with contextlib.suppress(Exception):
            storage.log(storage.data_dir(environ), f"prompt hook error {type(error).__name__}")
    return 0
