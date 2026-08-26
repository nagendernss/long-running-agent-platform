"""Generic signals: the Engine handles these itself, for every workflow type."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import field_validator

from app.clock import coerce_duration
from app.signals.base import Signal


class Reschedule(Signal):
    type: Literal["RESCHEDULE"] = "RESCHEDULE"
    wait_duration: str  # canonical compact form: "30m", "2h", "14d", "3w"
    reason: Optional[str] = None

    @field_validator("wait_duration")
    @classmethod
    def _canonical_duration(cls, value: str) -> str:
        """The scheduler assumes the compact form, so normalise here and reject what
        cannot be read. Keeps a sloppy brain from reaching the scheduler."""
        coerced = coerce_duration(value)
        if coerced is None:
            raise ValueError(f"unparseable wait_duration: {value!r}")
        return coerced


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
