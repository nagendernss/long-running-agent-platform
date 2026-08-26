from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.clock import coerce_duration, parse_duration
from app.db.models import Contact
from app.field_registry import FieldRegistry
from app.retry import resolve_retry_delay
from app.scheduling.constraints import apply_scheduling_constraints
from app.signals import GENERIC_SIGNAL_TYPES, EntityUpdate
from app.write_back import WriteBackResolver
from tests.helpers import load, medical_context, review_tasks


def test_parse_duration():
    assert parse_duration("14d") == timedelta(days=14)
    assert parse_duration("2h") == timedelta(hours=2)
    assert parse_duration("3w") == timedelta(weeks=3)
    assert parse_duration("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_coerce_duration_normalises_sloppy_brain_output():
    """A real Gemini reply once contained "14dPool filter set / schedule follow up in
    2 weeks". The scheduler must never see that, so Reschedule normalises or rejects."""
    assert coerce_duration("14d") == "14d"
    assert coerce_duration(" 3 W ") == "3w"
    assert coerce_duration("wait 5d please") == "5d"
    assert coerce_duration("two weeks") == "2w"
    assert coerce_duration("a couple of days") == "2d"
    assert coerce_duration("1 month") == "30d"
    assert coerce_duration("14dPool filter set / schedule follow up in 2 weeks") in {"14d", "2w"}
    assert coerce_duration("whenever") is None
    assert coerce_duration("") is None


def test_reschedule_signal_rejects_unschedulable_durations():
    from pydantic import ValidationError

    from app.signals import Reschedule

    assert Reschedule(wait_duration="two weeks").wait_duration == "2w"
    assert parse_duration(Reschedule(wait_duration="14d garbage text").wait_duration) == timedelta(days=14)
    with pytest.raises(ValidationError):
        Reschedule(wait_duration="sometime soon")


def test_retry_policy_resolution():
    policy = {"no_answer": {"schedule": ["2d", "5d", "14d"]}}
    assert [resolve_retry_delay(policy, i) for i in range(4)] == [
        timedelta(days=2), timedelta(days=5), timedelta(days=14), None,
    ]
    assert resolve_retry_delay({}, 0) is None


def test_scheduling_constraints_clamp_to_business_hours_and_skip_weekends():
    contact = Contact(name="x", role="provider", timezone="America/New_York", business_hours={"start": "09:00", "end": "17:00"})
    # Friday 2026-01-09 23:00 UTC = 18:00 ET (after hours) -> Monday 09:00 ET = 14:00 UTC
    raw = datetime(2026, 1, 9, 23, 0, tzinfo=timezone.utc)
    assert apply_scheduling_constraints(raw, contact) == datetime(2026, 1, 12, 14, 0, tzinfo=timezone.utc)
    # Tuesday 06:00 ET (before hours) -> same day 09:00 ET
    raw = datetime(2026, 1, 6, 11, 0, tzinfo=timezone.utc)
    assert apply_scheduling_constraints(raw, contact) == datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc)
    # inside hours -> unchanged
    raw = datetime(2026, 1, 6, 15, 30, tzinfo=timezone.utc)
    assert apply_scheduling_constraints(raw, contact) == raw
    # no contact -> defaults still apply
    assert apply_scheduling_constraints(datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc), None).weekday() == 0


def test_field_registry_threshold_lookup():
    reg = FieldRegistry.from_dict({"contact": {"phone": {"auto_apply_threshold": 0.85}}})
    assert reg.get("contact", "phone").auto_apply_threshold == 0.85
    assert reg.get("contact", "ssn") is None
    assert not reg.is_writable("case_record", "matter_type")


def test_generic_signal_types_are_the_engine_routing_key():
    assert GENERIC_SIGNAL_TYPES == {"RESCHEDULE", "NO_ANSWER", "ENTITY_UPDATE", "ACTION_REQUIRED", "NEEDS_HUMAN"}


async def test_low_confidence_fact_is_proposed_and_reviewed(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed))
        iid = inst.id
        await rt.engine.advance_instance(
            s, inst,
            [EntityUpdate(entity_type="contact", entity_id=str(seed.provider_id), field="phone", new_value="5559999", confidence=0.5)],
        )
    async with rt.session_factory() as s:
        from app.db.facts import get_current_fact, get_current_value, get_entity_field
        assert await get_current_value(s, "contact", seed.provider_id, "phone") is None  # nothing applied
        assert await get_entity_field(s, "contact", seed.provider_id, "phone") == seed.provider_phone
        assert await get_current_fact(s, "contact", seed.provider_id, "phone") is None
    assert (await load(rt, iid)).status == "blocked"
    assert [t.reason for t in await review_tasks(rt, iid)] == ["low_confidence_fact"]


async def test_unregistered_field_is_rejected(rt, seed):
    resolver = WriteBackResolver(FieldRegistry.from_dict({"contact": {"phone": {"auto_apply_threshold": 0.5}}}))
    async with rt.session_factory() as s, s.begin():
        fact = await resolver.apply(
            s, EntityUpdate(entity_type="contact", entity_id=str(seed.client_id), field="ssn", new_value="x", confidence=1.0),
            source_event_id=None, now=rt.clock.now(),
        )
        assert fact.status == "rejected"
        fact = await resolver.apply(
            s, EntityUpdate(entity_type="contact", entity_id=str(seed.client_id), field="phone", new_value="+1999", confidence=0.6),
            source_event_id=None, now=rt.clock.now(),
        )
        assert fact.status == "applied" and fact.old_value == seed.client_phone


async def test_attempt_idempotency_key(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed))
        key = f"{inst.id}:{uuid.uuid4().hex}"
        assert await rt.engine._run_attempt(s, inst, key=key, reason="t") is True
        assert await rt.engine._run_attempt(s, inst, key=key, reason="t") is False


def test_clamped_wakes_are_spread_so_the_day_does_not_open_with_one_burst():
    """Everything due overnight or over a weekend clamps to the same instant. At a
    thousand attempts a day that is hundreds of calls fired on one second, which the
    phone provider and the LLM will both rate-limit."""
    from app.scheduling.constraints import DEFAULT_SPREAD

    contact = Contact(name="x", role="provider", timezone="America/New_York",
                      business_hours={"start": "09:00", "end": "17:00"})
    friday_evening = datetime(2026, 1, 9, 23, 0, tzinfo=timezone.utc)  # 18:00 ET Friday

    times = [apply_scheduling_constraints(friday_evening, contact, key=f"instance-{i}") for i in range(50)]
    monday_open = datetime(2026, 1, 12, 14, 0, tzinfo=timezone.utc)     # 09:00 ET Monday

    assert len(set(times)) > 40, "50 instances must not land on a handful of instants"
    assert all(monday_open <= t < monday_open + DEFAULT_SPREAD for t in times), "and all inside the window"


def test_the_spread_is_stable_for_one_instance():
    """A wake recomputed twice must not hop around, or a matter would keep losing its
    place whenever anything rescheduled it."""
    contact = Contact(name="x", role="provider", timezone="America/New_York",
                      business_hours={"start": "09:00", "end": "17:00"})
    raw = datetime(2026, 1, 9, 23, 0, tzinfo=timezone.utc)
    assert apply_scheduling_constraints(raw, contact, key="abc") == apply_scheduling_constraints(raw, contact, key="abc")
    assert apply_scheduling_constraints(raw, contact, key="abc") != apply_scheduling_constraints(raw, contact, key="xyz")


def test_a_time_already_inside_business_hours_is_left_exactly_alone():
    """Only the pile-up at the window boundary is spread; a time the caller chose
    precisely is honoured."""
    contact = Contact(name="x", role="provider", timezone="America/New_York",
                      business_hours={"start": "09:00", "end": "17:00"})
    inside = datetime(2026, 1, 6, 15, 30, tzinfo=timezone.utc)
    assert apply_scheduling_constraints(inside, contact, key="anything") == inside


def test_maintenance_tasks_are_registered_to_run_on_their_own(rt):
    from app.scheduling.scheduler import CLEANUP_CRON, CLEANUP_TASK_NAME, RECOVERY_CRON, RECOVERY_TASK_NAME

    periodic = {p.task.name: p.cron for p in rt.procrastinate_app.periodic_registry.periodic_tasks.values()}
    assert periodic.get(RECOVERY_TASK_NAME) == RECOVERY_CRON
    assert periodic.get(CLEANUP_TASK_NAME) == CLEANUP_CRON, "finished jobs must be pruned or the queue grows forever"
