"""Being told the right number is a reason to call back now, not in three days.

Reported from a live call: the agent phoned, heard "wrong number, it's 555-0199",
wrote the corrected number down - and then sat on the response deadline armed for the
call that had gone to the old number. It had learned how to reach them and waited
three days to use it.
"""
from __future__ import annotations

from datetime import timedelta

from app.engine import RETRY_AFTER_CORRECTION
from app.signals import EntityUpdate, NeedsHuman, Reschedule
from tests.helpers import events, load, medical_context


async def start(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        return inst.id


async def advance(rt, iid, signals):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.advance_instance(s, inst, signals)


def correction(seed, field="phone", value="5550199", confidence=0.95) -> EntityUpdate:
    return EntityUpdate(
        entity_type="contact", entity_id=str(seed.provider_id), field=field,
        new_value=value, confidence=confidence, evidence="wrong number",
    )


async def test_a_corrected_number_is_called_back_within_minutes(rt, seed):
    iid = await start(rt, seed)
    assert (await load(rt, iid)).wake_reason == "response_timeout"

    await advance(rt, iid, [correction(seed)])

    inst = await load(rt, iid)
    assert inst.wake_reason == "retry_after_correction"
    assert inst.next_wake_at - rt.clock.now() <= RETRY_AFTER_CORRECTION + timedelta(minutes=1)
    assert "outcome_recorded" in await events(rt, iid)


async def test_a_corrected_email_or_channel_counts_too(rt, seed):
    for field, value in [("email", "roi@mercy.example"), ("preferred_channel", "email")]:
        iid = await start(rt, seed)
        await advance(rt, iid, [correction(seed, field=field, value=value)])
        assert (await load(rt, iid)).wake_reason == "retry_after_correction", field


async def test_a_field_that_is_not_about_reaching_them_changes_nothing(rt, seed):
    """Correcting a preferred contact time is not a reason to redial."""
    iid = await start(rt, seed)
    before = await load(rt, iid)

    await advance(rt, iid, [correction(seed, field="preferred_contact_time", value="afternoons", confidence=0.95)])

    inst = await load(rt, iid)
    assert inst.wake_reason == "response_timeout" and inst.next_wake_at == before.next_wake_at


async def test_being_asked_to_call_back_later_wins(rt, seed):
    """"Wrong number, it's 555-0199, and don't call until next week" must wait a week,
    whichever order the two signals arrive in."""
    iid = await start(rt, seed)
    await advance(rt, iid, [Reschedule(wait_duration="7d"), correction(seed)])

    inst = await load(rt, iid)
    assert inst.wake_reason == "dynamic_reschedule"
    assert inst.next_wake_at - rt.clock.now() >= timedelta(days=7)


async def test_a_correction_does_not_wake_an_instance_waiting_on_a_person(rt, seed):
    iid = await start(rt, seed)
    await advance(rt, iid, [NeedsHuman(reason="caller_requested_human")])
    assert (await load(rt, iid)).status == "blocked"

    await advance(rt, iid, [correction(seed)])
    inst = await load(rt, iid)
    assert inst.status == "blocked" and inst.next_wake_at is None, "a human is dealing with it"


async def test_a_retry_already_scheduled_is_left_alone(rt, seed):
    """After a no-answer the ladder has chosen when to try again; a correction that
    arrives afterwards should not quietly shorten it."""
    iid = await start(rt, seed)
    await advance(rt, iid, [__import__("app.signals", fromlist=["NoAnswer"]).NoAnswer()])
    scheduled = await load(rt, iid)
    assert scheduled.wake_reason == "retry"

    await advance(rt, iid, [correction(seed)])
    inst = await load(rt, iid)
    assert inst.wake_reason == "retry" and inst.next_wake_at == scheduled.next_wake_at


async def test_correcting_somebody_elses_details_does_not_redial(rt, seed):
    """The client's new email is not a reason to ring the provider back."""
    iid = await start(rt, seed)
    before = await load(rt, iid)

    await advance(rt, iid, [EntityUpdate(
        entity_type="contact", entity_id=str(seed.client_id), field="phone",
        new_value="5551234", confidence=0.95, evidence="my new number",
    )])

    inst = await load(rt, iid)
    assert inst.wake_reason == "response_timeout" and inst.next_wake_at == before.next_wake_at


async def test_a_low_confidence_correction_waits_for_a_human(rt, seed):
    """It is not applied yet, so there is nothing new to dial."""
    iid = await start(rt, seed)
    await advance(rt, iid, [correction(seed, confidence=0.4)])

    inst = await load(rt, iid)
    assert inst.status == "blocked", "held for confirmation"
    assert inst.wake_reason != "retry_after_correction"
