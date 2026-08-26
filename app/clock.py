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


class OffsetClock:
    """Real time plus an offset you can push forward - the dev server's time machine.

    A FakeClock freezes time, which is right for tests but wrong for a running server
    (nothing would ever tick on its own). This one keeps moving at real speed while
    letting an operator jump the clock forward to see a 14-day wait fire now.

    Scheduling stays honest: a wake is stored at virtual-now + delay, so after jumping
    forward the same due-check that runs in production is what fires it.
    """

    def __init__(self, offset: timedelta | None = None):
        self.offset = offset or timedelta()

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def advance(self, delta: timedelta) -> datetime:
        self.offset += delta
        return self.now()

    def advance_to(self, at: datetime) -> datetime:
        """Jump to a specific virtual instant (used for 'skip to the next wake')."""
        if at > self.now():
            self.offset += at - self.now()
        return self.now()

    def reset(self) -> datetime:
        self.offset = timedelta()
        return self.now()


_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)

WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
}
UNIT_TO_SUFFIX = {"minute": "m", "hour": "h", "day": "d", "week": "w"}
_LOOSE_COMPACT_RE = re.compile(r"(\d+)\s*([mhdw])\b", re.IGNORECASE)
_LOOSE_WORDS_RE = re.compile(
    r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few)\s*(?:of\s+)?"
    r"(minutes?|hours?|days?|weeks?|months?)",
    re.IGNORECASE,
)


def parse_duration(text: str) -> timedelta:
    """'14d' -> 14 days, '2h' -> 2 hours, '3w' -> 21 days, '30m' -> 30 minutes."""
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"unparseable duration: {text!r}")
    return timedelta(**{_UNITS[m.group(2).lower()]: int(m.group(1))})


def duration_from_words(amount: str, unit: str) -> str:
    """('two', 'weeks') -> '2w'. Months are normalised to days."""
    n = int(amount) if amount.isdigit() else WORD_NUMBERS.get(amount.lower(), 1)
    unit = unit.lower().rstrip("s")
    if unit == "month":
        return f"{n * 30}d"
    return f"{n}{UNIT_TO_SUFFIX[unit]}"


def coerce_duration(text: str) -> str | None:
    """Best-effort normalisation of whatever a brain produced into the canonical form.

    An LLM will occasionally return '14d schedule follow up in 2 weeks' or plain
    'two weeks'. Everything downstream (the scheduler, the retry ladder) assumes the
    compact form, so the coercion happens once, here, and anything that cannot be
    read is rejected rather than guessed at.
    """
    if not text:
        return None
    text = text.strip()
    if _DURATION_RE.match(text):
        return text.replace(" ", "").lower()
    m = _LOOSE_COMPACT_RE.search(text)
    if m:
        return f"{int(m.group(1))}{m.group(2).lower()}"
    m = _LOOSE_WORDS_RE.search(text)
    if m:
        return duration_from_words(m.group(1), m.group(2))
    return None
