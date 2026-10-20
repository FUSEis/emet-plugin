"""The plugin's private folder: per-session state, the retry queue, status and log."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

LOG_MAX_BYTES = 1_000_000


def data_dir(environ: Mapping[str, str] = os.environ) -> Path:
    root = Path(environ.get("CLAUDE_PLUGIN_DATA") or Path.home() / ".emet" / "claude-code")
    for path in (root, root / "state", root / "queue"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def write_private(path: Path, text: str) -> None:
    """Writes atomically with owner-only permissions, since files can hold session content."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temp, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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


def log(root: Path, message: str) -> None:
    """Appends one line. Never content: ids, counts and HTTP statuses only."""
    path = root / "emet.log"
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, root / "emet.log.1")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except OSError:
        pass
