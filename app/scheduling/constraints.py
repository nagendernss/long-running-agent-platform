"""Scheduling constraints: shift a raw wake time into the contact's timezone and clamp
it into their business hours. Compliance / legal calling-window logic is a stub.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db.models import Contact

DEFAULT_TZ = "America/New_York"
DEFAULT_HOURS = {"start": "09:00", "end": "17:00"}


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def apply_scheduling_constraints(raw_time: datetime, contact: Contact | None) -> datetime:
    """Return the earliest time >= raw_time that falls inside the contact's business
    hours (in their timezone). Weekends are skipped. Result is tz-aware UTC."""
    if raw_time.tzinfo is None:
        raw_time = raw_time.replace(tzinfo=timezone.utc)
    tz = ZoneInfo((contact.timezone if contact and contact.timezone else DEFAULT_TZ))
    hours = (contact.business_hours if contact and contact.business_hours else None) or DEFAULT_HOURS
    start, end = _parse_hhmm(hours["start"]), _parse_hhmm(hours["end"])

    local = raw_time.astimezone(tz)
    for _ in range(14):  # never loop forever on a degenerate config
        if local.weekday() >= 5:  # Sat/Sun -> next Monday at start
            local = (local + timedelta(days=7 - local.weekday())).replace(
                hour=start.hour, minute=start.minute, second=0, microsecond=0
            )
            continue
        if local.time() < start:
            local = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            break
        if local.time() >= end:
            local = (local + timedelta(days=1)).replace(
                hour=start.hour, minute=start.minute, second=0, microsecond=0
            )
            continue
        break
    local = apply_compliance_window(local, contact)
    return local.astimezone(timezone.utc)


def apply_compliance_window(local_time: datetime, contact: Contact | None) -> datetime:
    """STUB: legal/compliance calling windows (TCPA, state rules, do-not-call lists).
    Intentionally a pass-through in this slice - see README "Known gaps"."""
    return local_time
