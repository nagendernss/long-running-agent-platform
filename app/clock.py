"""Injectable clock so scheduling logic is testable and the demo can time-travel."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    def __init__(self, start: datetime | None = None):
        # Monday 2026-01-05 10:00 America/New_York
        self._now = start or datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def set(self, at: datetime) -> None:
        self._now = at


_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)


def parse_duration(text: str) -> timedelta:
    """'14d' -> 14 days, '2h' -> 2 hours, '3w' -> 21 days, '30m' -> 30 minutes."""
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"unparseable duration: {text!r}")
    return timedelta(**{_UNITS[m.group(2).lower()]: int(m.group(1))})
