"""The plugin's private folder: per-session state, the retry queue, status and log."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

LOG_MAX_BYTES = 1_000_000
# No line the plugin writes is near this long, so a longer one holds text from somewhere else
LOG_LINE_MAX = 300
# Makes an open fail on a symbolic link. Windows has no such flag
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# For reading: no link is followed, a pipe put in the file's place cannot make the open wait, and Windows changes no byte
READ_FLAGS = os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


def data_dir(environ: Mapping[str, str] = os.environ) -> Path:
    root = Path(environ.get("CLAUDE_PLUGIN_DATA") or Path.home() / ".emet" / "claude-code")
    for path in (root, root / "state", root / "queue"):
        # A link in place of a folder the plugin makes is removed, never written through. The root can be a link by choice
        if path != root and path.is_symlink():
            path.unlink()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir leaves the mode of a folder that already existed alone
        if sys.platform != "win32" and path.stat().st_mode & 0o077:
            path.chmod(0o700)
    return root


def write_private(path: Path, text: str) -> None:
    """Writes atomically with owner-only permissions, since files can hold session content.

    Never writes through a symbolic link: the temporary file is always a new one, and the rename replaces a link at path itself.
    """
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW
    try:
        try:
            fd = os.open(str(temp), flags, 0o600)
        except FileExistsError:
            # Left by a crashed run with this process id, or planted. Removing a link leaves its target alone
            temp.unlink()
            fd = os.open(str(temp), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except OSError:
        # A write cut short must not leave the content behind under another name
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def read_json(path: Path, default: Any) -> Any:
    """Returns what the file holds, or default when it cannot be read. A symbolic link is not read where the platform can refuse one."""
    try:
        with os.fdopen(os.open(str(path), READ_FLAGS), "r", encoding="utf-8") as handle:
            return json.loads(handle.read())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    write_private(path, json.dumps(value, ensure_ascii=False))


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Holds an exclusive lock on path, so two hooks never process one session at once."""
    with open(path, "a+") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def try_locked(path: Path) -> Iterator[bool]:
    """Takes the lock on path only when no one holds it, and never waits. Yields whether it was taken."""
    with open(path, "a+") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def now(at: Optional[float] = None) -> str:
    """Writes the current time, or the one at seconds since the epoch, the one way the plugin's files hold a time."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(at))


def one_line(message: str) -> str:
    """Returns the message with every line break, control and invisible character replaced, cut to the length of a log line.

    A value read from a file or the network can so never start a line of its own.
    """
    kept = (
        "?" if unicodedata.category(character)[0] == "C" or character in "\u2028\u2029" else character
        for character in message[:LOG_LINE_MAX]
    )
    return "".join(kept)


def log(root: Path, message: str) -> None:
    """Appends one line. Never content: ids, counts and HTTP statuses only."""
    path = root / "emet.log"
    message = one_line(message)
    try:
        # A link in the log's place is removed, never written through. The flag below closes the gap after this check
        if path.is_symlink():
            path.unlink()
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, root / "emet.log.1")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"{now()} {message}\n")
    except OSError:
        pass
