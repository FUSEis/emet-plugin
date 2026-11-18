"""The pause after Emet could not be reached. While it lasts no request is made, and turns wait in the queue.

One small file shared by every session on the machine, so one failed request spares the others the wait.
"""

from __future__ import annotations

import contextlib
import random
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Optional

from . import storage, timestamps

FIRST_S = 30
LONGEST_S = 600
# The longest wait taken from Emet's Retry-After, and the longest a pause file is believed
RETRY_AFTER_MAX_S = 3600
# Machines that failed together do not retry together
SPREAD = (0.8, 1.2)


def clock() -> float:
    return time.time()


def spread() -> float:
    return random.uniform(*SPREAD)  # noqa: S311 - timing, not a secret


def path(root: Path) -> Path:
    return root / "pause.json"


def read(root: Path) -> Dict[str, Any]:
    saved = storage.read_json(path(root), {})
    return saved if isinstance(saved, dict) else {}


def until(root: Path) -> str:
    """Returns the time before which no request is made, or "" when no pause is in force."""
    saved = read(root).get("until")
    at = timestamps.parse(saved)
    if at is None:
        return ""
    left = at.timestamp() - clock()
    # A time further away than any pause this module writes is damage, not a pause
    return str(saved) if 0 < left <= RETRY_AFTER_MAX_S + 1 else ""


def named(header: Optional[str]) -> Optional[float]:
    """Returns the seconds a Retry-After header asks for, as seconds or as a date, or None when it names none."""
    value = (header or "").strip()
    if value.isascii() and value.isdigit():
        return float(min(int(value), RETRY_AFTER_MAX_S))
    try:
        at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return min(max(at.timestamp() - clock(), 0.0), float(RETRY_AFTER_MAX_S))


def extend(root: Path, retry_after: Optional[float] = None) -> None:
    """Starts the pause after a failed request, twice as long as the last one unless Emet named the wait. Never raises."""
    try:
        with storage.locked(root / "pause.lock"):
            # Another worker already recorded this failure
            if until(root):
                return
            last = read(root).get("delay")
            doubled = last * 2 if isinstance(last, (int, float)) and not isinstance(last, bool) and last > 0 else FIRST_S
            delay = min(max(doubled, FIRST_S), LONGEST_S)
            wait = min(delay * spread(), LONGEST_S) if retry_after is None else min(retry_after, RETRY_AFTER_MAX_S)
            storage.write_json(path(root), {"until": storage.now(clock() + wait), "delay": delay})
            storage.log(root, f"paused {int(wait)}s")
    except OSError as error:
        storage.log(root, f"pause not written {type(error).__name__}")


def clear(root: Path) -> None:
    """Ends the pause once Emet answered."""
    with contextlib.suppress(OSError):
        path(root).unlink()
