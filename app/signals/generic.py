"""Generic signals: the Engine handles these itself, for every workflow type."""
from __future__ import annotations

from typing import Literal, Optional

from app.signals.base import Signal


class Reschedule(Signal):
    type: Literal["RESCHEDULE"] = "RESCHEDULE"
    wait_duration: str  # e.g. "14d", "2h"
    reason: Optional[str] = None


class NoAnswer(Signal):
    type: Literal["NO_ANSWER"] = "NO_ANSWER"


class EntityUpdate(Signal):
    type: Literal["ENTITY_UPDATE"] = "ENTITY_UPDATE"
    entity_type: str
    entity_id: str
    field: str
    new_value: str


class NeedsHuman(Signal):
    type: Literal["NEEDS_HUMAN"] = "NEEDS_HUMAN"
    reason: str
    suggested_options: list[str] = []


GENERIC_SIGNAL_TYPES: frozenset[str] = frozenset(
    {"RESCHEDULE", "NO_ANSWER", "ENTITY_UPDATE", "NEEDS_HUMAN"}
)
