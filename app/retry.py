"""Retry policy resolution. Policies are plain dicts declared on WorkflowDefinitions:

    {"no_answer": {"schedule": ["2d", "5d", "14d"]}}

attempt_count is the number of attempts already made *without* a reply. Once the
schedule is exhausted, `resolve_retry_delay` returns None => escalate to a human.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.clock import parse_duration


def resolve_retry_delay(policy: dict[str, Any], attempt_count: int, kind: str = "no_answer") -> timedelta | None:
    schedule: list[str] = (policy.get(kind) or {}).get("schedule") or []
    if attempt_count < len(schedule):
        return parse_duration(schedule[attempt_count])
    return None
