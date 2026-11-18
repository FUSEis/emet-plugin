"""Reads the times in a transcript as instants, and writes them the one way Emet is sent them.

Parsed by hand: datetime.fromisoformat accepts different forms on Python 3.9 and on 3.11 and later,
so the same session could be read two ways.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Optional

# A date, a time with up to nine fraction digits, and a zone: Z, +HH:MM, +HHMM or +HH
FORM = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?(?:(Z)|([+-])([0-9]{2})(?::?([0-9]{2}))?)"
)


class Unreadable(ValueError):
    """Entries that hold no time that can be read."""


def parse(value: Any) -> Optional[datetime]:
    """Returns the UTC instant a transcript time names, or None when it cannot be read.

    A time with no zone is not read: assuming UTC could move a trace by hours without anyone noticing.
    """
    match = FORM.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, utc, sign, zone_hours, zone_minutes = match.groups()
    try:
        # Digits past the sixth are dropped, not rounded
        at = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), int((fraction or "0").ljust(6, "0")[:6]))
        if utc is None:
            hours, minutes = int(zone_hours), int(zone_minutes or 0)
            if hours > 23 or minutes > 59:
                return None
            offset = timedelta(hours=hours, minutes=minutes)
            at = at + offset if sign == "-" else at - offset
    except (ValueError, OverflowError):
        return None
    return at.replace(tzinfo=timezone.utc)


def of(entries: Iterable[Any]) -> List[datetime]:
    """Returns the readable times of the entries, in file order."""
    found = []
    for entry in entries:
        at = parse(entry.get("timestamp")) if isinstance(entry, dict) else None
        if at is not None:
            found.append(at)
    return found


def text(at: datetime) -> str:
    """Writes an instant as Claude Code writes it: UTC, milliseconds, Z."""
    return f"{at.year:04d}-{at.month:02d}-{at.day:02d}T{at.hour:02d}:{at.minute:02d}:{at.second:02d}.{at.microsecond // 1000:03d}Z"
