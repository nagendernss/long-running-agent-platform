"""Calls as rows: placed, answered, transcribed a turn at a time, finished."""
from __future__ import annotations

import pytest

from app.voice.repository import (
    answer_call,
    append_turn,
    finish_call,
    live_call_for_instance,
    place_call,
    ringing_calls,
    transcript_text,
)


async def place(rt, seed, instance_id=None):
    async with rt.session_factory() as s, s.begin():
        call = await place_call(
            s, instance_id=instance_id, contact_id=seed.provider_id,
            goal="Find out if the records were sent.", opening="Hi, calling about the records.",
            to_address="+15550100", now=rt.clock.now(),
        )
        return call.id


async def test_a_call_starts_ringing_and_is_waiting_to_be_answered(rt, seed):
    call_id = await place(rt, seed)
    async with rt.session_factory() as s:
        waiting = await ringing_calls(s)
    assert [c.id for c in waiting] == [call_id]
    assert waiting[0].to_address == "+15550100" and waiting[0].transcript == []


async def test_answering_moves_it_to_active_once(rt, seed):
    call_id = await place(rt, seed)
    async with rt.session_factory() as s, s.begin():
        assert (await answer_call(s, call_id, rt.clock.now())).status == "active"
    async with rt.session_factory() as s, s.begin():
        assert await answer_call(s, call_id, rt.clock.now()) is None, "a second answer is a no-op"


async def test_turns_are_appended_in_order_and_survive_separately(rt, seed):
    """Written a turn at a time so a call that drops halfway keeps what was said."""
    call_id = await place(rt, seed)
    async with rt.session_factory() as s, s.begin():
        await append_turn(s, call_id, "agent", "Hi, calling about the records.", rt.clock.now())
    async with rt.session_factory() as s, s.begin():
        await append_turn(s, call_id, "contact", "We need an authorization first.", rt.clock.now())
    async with rt.session_factory() as s, s.begin():
        await append_turn(s, call_id, "agent", "Where should we send it?", rt.clock.now(), source="gemini")

    async with rt.session_factory() as s:
        call = (await ringing_calls(s))[0]
    assert [t["who"] for t in call.transcript] == ["agent", "contact", "agent"]
    assert call.transcript[1]["text"] == "We need an authorization first."
    assert call.transcript[2]["source"] == "gemini"


async def test_only_the_other_party_counts_as_evidence(rt, seed):
    """The brain must not read the agent's own words back: its guesses would become
    extracted facts."""
    call_id = await place(rt, seed)
    async with rt.session_factory() as s, s.begin():
        await append_turn(s, call_id, "agent", "Is there a fee of forty five dollars?", rt.clock.now())
        await append_turn(s, call_id, "contact", "No, there is no fee.", rt.clock.now())
    async with rt.session_factory() as s:
        call = (await ringing_calls(s))[0]
    assert transcript_text(call) == "No, there is no fee."


async def test_finishing_is_idempotent_because_a_drop_and_a_hangup_both_arrive(rt, seed):
    call_id = await place(rt, seed)
    async with rt.session_factory() as s, s.begin():
        assert (await finish_call(s, call_id, "completed", rt.clock.now())).status == "completed"
    async with rt.session_factory() as s, s.begin():
        again = await finish_call(s, call_id, "missed", rt.clock.now())
    assert again.status == "completed", "the first outcome stands"


async def test_a_finished_call_is_no_longer_ringing(rt, seed):
    call_id = await place(rt, seed)
    async with rt.session_factory() as s, s.begin():
        await finish_call(s, call_id, "missed", rt.clock.now())
    async with rt.session_factory() as s:
        assert await ringing_calls(s) == []


async def test_only_final_statuses_can_finish_a_call(rt, seed):
    call_id = await place(rt, seed)
    with pytest.raises(ValueError):
        async with rt.session_factory() as s, s.begin():
            await finish_call(s, call_id, "active", rt.clock.now())


async def test_one_live_call_per_instance(rt, seed):
    """A second call would talk over the first."""
    from tests.helpers import medical_context

    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        iid = inst.id

    async with rt.session_factory() as s:
        assert await live_call_for_instance(s, iid) is None

    call_id = await place(rt, seed, instance_id=iid)
    async with rt.session_factory() as s:
        assert (await live_call_for_instance(s, iid)).id == call_id

    async with rt.session_factory() as s, s.begin():
        await finish_call(s, call_id, "completed", rt.clock.now())
    async with rt.session_factory() as s:
        assert await live_call_for_instance(s, iid) is None, "a finished call frees the line"
