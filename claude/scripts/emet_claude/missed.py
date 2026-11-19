"""Gives Claude a flagged verdict whose wake was held back or lost, with the next prompt the user sends.

Runs in the foreground at every prompt (UserPromptSubmit), so the session waits for it. It reads
the plugin's own files and the end of the session file, makes no request and waits for no lock.
It always exits 0. It prints nothing, or one JSON object that adds context for Claude and one line
for the user. It never sets a field that can block or change the prompt.

The checks file decides, and the verdict file only supplies the text. A verdict is shown when
its `flagged` line says Claude was not woken ("handed" false), or says it was and the session file
holds no trace of that wake. It is shown once, in the session that produced it, one per prompt,
the newest first, and at most SHOW_CAP times in a session. The showing is recorded as `shown`
before anything is printed, and the file is removed then, so a crash can lose a showing and never
repeat one.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from . import pause, pending, report, storage, timestamps, transcript, watch

EVENT = "UserPromptSubmit"
# Showings per session, counted apart from the wakes: a report held back by the wake cap is shown here
SHOW_CAP = 3
# A wake handed over less than this long ago may not be in the session file yet, so it is not judged
HANDED_GRACE_S = 10
# What a slash command and a shell escape begin with. No model call answers most of them
NOT_A_PROMPT = ("/", "!")
# Shown to the user by Claude Code (systemMessage). Fixed words, never text from a report
USER_LINE = "Emet disputed a claim in an earlier answer of this session. Claude was given Emet's report with this prompt."

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


def woken(entries: Entries, handed_at: float) -> Optional[bool]:
    """Says whether the session file holds the wake that was handed over at that time. None when it cannot tell.

    True when an entry written since then holds the wake's opening sentence, as a turn of its own or folded into
    a running one. False only when the part of the file that was read begins before the hand-over and holds none.
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
        # Claude Code can wrap the lines again
        elif any(report.WAKE_OPENING in " ".join(text.split()) for text in strings(entry)):
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


def recorded(found: List[Dict[str, Any]], verdict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Returns the `flagged` line of the report the verdict was saved for, or None when the poller recorded none."""
    key = (verdict["trace_uuid"], verdict["report_uuid"], verdict["revision"])
    lines = [
        item
        for item in found
        if item.get("outcome") == watch.FLAGGED and (item.get("trace_uuid"), item.get("report_uuid"), item.get("revision")) == key
    ]
    return lines[-1] if lines else None


def remove(root: Path, verdict: Dict[str, Any], why: str = "") -> None:
    """Removes a verdict's file, so no report text rests on disk past its use. Never raises."""
    try:
        watch.verdict_path(root, verdict["session_id"], verdict["trace_uuid"], verdict["revision"]).unlink()
    except OSError as error:
        # The record decides what is shown. A file that stays is removed at a later prompt, or once it is too old
        storage.log(root, f"verdict {verdict['trace_uuid']} not removed {type(error).__name__}")
        return
    if why:
        storage.log(root, f"verdict {verdict['trace_uuid']} removed, {why}")


def missed(line: Dict[str, Any], entries: Entries) -> Optional[bool]:
    """Says whether Claude was never given the verdict of this `flagged` line. None when that cannot be told, and the file waits."""
    if line.get("handed") is False:
        return True
    at = timestamps.parse(line.get("at"))
    if line.get("handed") is not True or at is None:
        return None
    found = woken(entries, at.timestamp())
    return None if found is None else not found


def due(root: Path, session_id: str, saved: List[Dict[str, Any]], entries: Entries) -> List[Dict[str, Any]]:
    """Returns the session's verdicts that can be shown now, newest first, and removes the files that never will be."""
    found = watch.checks(root, session_id)
    told = {item.get("trace_uuid") for item in found if item.get("outcome") == watch.SHOWN}
    waiting = []
    for verdict in saved:
        line = recorded(found, verdict)
        if verdict["trace_uuid"] in told:
            # Left by a showing that could not remove it
            remove(root, verdict)
            continue
        if line is None:
            # Not recorded: the poller settles this report again, and may still wake Claude with it
            continue
        never_given = missed(line, entries)
        if never_given is False:
            remove(root, verdict, "Claude was woken with it")
        elif compacted(entries, saved_at(verdict)):
            remove(root, verdict, "the conversation was compacted since")
        elif never_given:
            waiting.append(verdict)
    if waiting and len([item for item in found if item.get("outcome") == watch.SHOWN]) >= SHOW_CAP:
        for verdict in waiting:
            remove(root, verdict, f"the session already had {SHOW_CAP} showings")
        return []
    return sorted(waiting, key=lambda verdict: (saved_at(verdict), verdict["trace_uuid"]), reverse=True)


def choose(raw: bytes, environ: Mapping[str, str]) -> str:
    """Returns the text to give Claude with this prompt, or "" when there is none. The verdict is recorded and its file removed first."""
    hook_input = storage.loads(raw or b"{}")
    if not isinstance(hook_input, dict):
        return ""
    session_id = str(hook_input.get("session_id") or "")
    # The session id names files
    if not pending.SESSION_ID.fullmatch(session_id):
        return ""
    root = storage.data_dir(environ)
    # Nearly every prompt ends here, after one look at a folder
    saved = watch.verdicts(root, session_id)
    if not saved:
        return ""
    if not carries(hook_input):
        storage.log(root, "verdict not shown, the prompt is a command or a notice")
        return ""
    # The poller knows the turn that carried a verdict by this id, and never tells Claude of its report
    prompt_id = pending.plain(hook_input.get("prompt_id"))
    if not prompt_id:
        storage.log(root, "verdict not shown, the prompt names no id")
        return ""
    entries, _ = watch.session_entries(str(hook_input.get("transcript_path") or ""))
    if watch.headless(entries, environ):
        storage.log(root, "verdict not shown, no person is at the session")
        return ""
    waiting = due(root, session_id, saved, entries)
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
    if not watch.record(root, session_id, verdict["trace_uuid"], watch.SHOWN, ids, wait=False):
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
