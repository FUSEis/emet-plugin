"""The saved verdicts: one file under verdicts/ for each flagged report, written by the poller and read back whole or not at all."""

from __future__ import annotations

import contextlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import pause, pending, report, storage, timestamps
from .config import endpoint_problem

UUID_FORM = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The name of a saved verdict: the session id, the trace and the revision of the report
VERDICT_NAME = re.compile(rf"[A-Za-z0-9_-]{{1,100}}\.{UUID_FORM.pattern}\.[0-9]{{1,7}}\.json")
# The name storage.write_private gives a verdict while it is written
VERDICT_TEMP_NAME = re.compile(rf"\.{VERDICT_NAME.pattern}\.[0-9]{{1,10}}\.tmp")


# A temporary file of a verdict left by a write cut short is removed once it is this old
STALE_TEMP_S = 24 * 3600


VERDICT_DAYS = 7


# The format of a saved verdict. A file that names no version is read as 1
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
