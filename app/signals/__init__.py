"""Shared signal vocabulary.

Generic signals are handled by the Engine itself. Domain signals live next to the
workflow that owns them (app/workflows/<name>.py) and are only ever routed to that
workflow's `handle_domain_signal`.
"""
from app.signals.base import Signal
from app.signals.generic import (
    GENERIC_SIGNAL_TYPES,
    EntityUpdate,
    NeedsHuman,
    NoAnswer,
    Reschedule,
)

__all__ = [
    "Signal",
    "GENERIC_SIGNAL_TYPES",
    "EntityUpdate",
    "NeedsHuman",
    "NoAnswer",
    "Reschedule",
]
