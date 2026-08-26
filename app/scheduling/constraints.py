"""Scheduling constraints: shift a raw wake time into the contact's timezone and clamp
it into their business hours. Compliance / legal calling-window logic is a stub.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db.models import Contact

DEFAULT_TZ = "America/New_York"
DEFAULT_HOURS = {"start": "09:00", "end": "17:00"}
# Everything that comes due overnight or at a weekend clamps to the same instant -
# 09:00 sharp - so the day opens with one burst against the phone provider and the
# LLM rather than a stream. Spread those over the first half hour.
DEFAULT_SPREAD = timedelta(minutes=30)


def _spread_offset(key: str | None, spread: timedelta) -> timedelta:
    """Deterministic per key, so a wake does not hop around every time it is
    recomputed, and a given matter keeps its slot in the queue."""
    if not key or spread <= timedelta(0):
        return timedelta(0)
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    return timedelta(seconds=int.from_bytes(digest, "big") % int(spread.total_seconds()))


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def apply_scheduling_constraints(
    raw_time: datetime,
    contact: Contact | None,
    *,
    spread: timedelta = DEFAULT_SPREAD,
    key: str | None = None,
) -> datetime:
    """Return the earliest time >= raw_time that falls inside the contact's business
    hours (in their timezone). Weekends are skipped. Result is tz-aware UTC.

    When the time had to be moved to the start of a working window, a deterministic
    offset within `spread` is added so a night's worth of due wakes does not all fire
    on the same second. A time that was already inside business hours is left exactly
    as the caller asked for it.
    """
    if raw_time.tzinfo is None:
        raw_time = raw_time.replace(tzinfo=timezone.utc)
    tz = ZoneInfo((contact.timezone if contact and contact.timezone else DEFAULT_TZ))
    hours = (contact.business_hours if contact and contact.business_hours else None) or DEFAULT_HOURS
    start, end = _parse_hhmm(hours["start"]), _parse_hhmm(hours["end"])

    local = raw_time.astimezone(tz)
    clamped = False
    for _ in range(14):  # never loop forever on a degenerate config
        if local.weekday() >= 5:  # Sat/Sun -> next Monday at start
            local = (local + timedelta(days=7 - local.weekday())).replace(
                hour=start.hour, minute=start.minute, second=0, microsecond=0
            )
            clamped = True
            continue
        if local.time() < start:
            local = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            clamped = True
            break
        if local.time() >= end:
            local = (local + timedelta(days=1)).replace(
                hour=start.hour, minute=start.minute, second=0, microsecond=0
            )
            clamped = True
            continue
        break
    if clamped:
        local += _spread_offset(key, spread)
    local = apply_compliance_window(local, contact)
    return local.astimezone(timezone.utc)


def apply_compliance_window(local_time: datetime, contact: Contact | None) -> datetime:
    """STUB: legal/compliance calling windows (TCPA, state rules, do-not-call lists).
    Intentionally a pass-through in this slice - see README "Known gaps"."""
    return local_time
