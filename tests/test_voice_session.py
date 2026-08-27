"""A whole call, without a microphone or a model.

The session takes its ear and its mouth as dependencies, so the suite can drive the
real turn loop with scripted parts. What it proves is the join: that a spoken
conversation ends up in the same extraction path as a typed reply, and that an
unanswered call reaches the existing retry ladder rather than a new one.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.channels.voice import VoiceChannel
from app.db.models import CallRow
from app.voice.agent import ScriptedCallAgent
from app.voice.session import DID_NOT_CATCH, VoiceCallSession, miss_call
from app.voice.stt import ScriptedSTT
from tests.helpers import events, load, medical_context, review_tasks


async def place(rt, seed):
    rt.engine.channel = VoiceChannel(rt.channel, rt.clock, registry=rt.registry)
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        iid = inst.id
    async with rt.session_factory() as s:
        call = (await s.execute(select(CallRow).where(CallRow.instance_id == iid))).scalar_one()
    return iid, call.id


def session_for(rt, call_id, heard, says):
    return VoiceCallSession(
        call_id=call_id, stt=ScriptedSTT(heard), agent=ScriptedCallAgent(says),
        session_factory=rt.session_factory, clock=rt.clock, engine=rt.engine,
    )


async def call_row(rt, call_id) -> CallRow:
    async with rt.session_factory() as s:
        return await s.get(CallRow, call_id)


async def test_a_whole_call_becomes_signals(rt, seed):
    """The join that matters: what was said out loud goes through the same door as a
    typed reply, and comes out as the same signals."""
    iid, call_id = await place(rt, seed)
    session = session_for(
        rt, call_id,
        heard=["We need a signed authorization before we send those."],
        says=["Understood - where should we send it?"],
    )

    opening = await session.on_answer()
    assert opening.startswith("Hi Mercy Hospital Records")

    turn = await session.on_utterance(b"audio-bytes")
    assert turn.say == "Understood - where should we send it?" and turn.done is False

    await session.on_hangup()

    call = await call_row(rt, call_id)
    assert call.status == "completed"
    assert [t["who"] for t in call.transcript] == ["agent", "contact", "agent"]

    inst = await load(rt, iid)
    assert inst.state == "awaiting_client_auth", "AUTH_REQUIRED, extracted from speech"
    assert "auth_required" in [t.reason for t in await review_tasks(rt, iid)]
    assert "call_answered" in await events(rt, iid) and "call_ended" in await events(rt, iid)


async def test_the_agent_hears_only_the_other_party(rt, seed):
    """Its own words are context, not evidence - otherwise a question it asked could
    come back as an extracted fact."""
    iid, call_id = await place(rt, seed)
    agent = ScriptedCallAgent(["Right."])
    session = VoiceCallSession(
        call_id=call_id, stt=ScriptedSTT(["There is a forty five dollar fee."]), agent=agent,
        session_factory=rt.session_factory, clock=rt.clock, engine=rt.engine,
    )
    await session.on_answer()
    await session.on_utterance(b"a")
    await session.on_hangup()

    inst = await load(rt, iid)
    requirement = inst.context.get("pending_requirement")
    assert requirement and requirement["action_type"] == "payment", "the fee was heard and acted on"


async def test_silence_asks_them_to_repeat_rather_than_inventing(rt, seed):
    _iid, call_id = await place(rt, seed)
    agent = ScriptedCallAgent(["should not be reached"])
    session = VoiceCallSession(
        call_id=call_id, stt=ScriptedSTT([""]), agent=agent,
        session_factory=rt.session_factory, clock=rt.clock, engine=rt.engine,
    )
    await session.on_answer()
    turn = await session.on_utterance(b"noise")

    assert turn.say == DID_NOT_CATCH and turn.done is False
    assert agent.seen == [], "the agent was never asked to reply to silence"


async def test_it_gives_up_rather_than_looping_on_a_bad_line(rt, seed):
    _iid, call_id = await place(rt, seed)
    session = session_for(rt, call_id, heard=["", "", ""], says=[])
    await session.on_answer()
    assert (await session.on_utterance(b"")).done is False
    assert (await session.on_utterance(b"")).done is False
    third = await session.on_utterance(b"")
    assert third.done is True and "call you back" in third.say


async def test_a_dropped_socket_still_submits_what_was_said(rt, seed):
    """Browser closed mid-call: the transcript so far is still evidence."""
    iid, call_id = await place(rt, seed)
    session = session_for(rt, call_id, heard=["Records were faxed this morning."], says=["Thank you."])
    await session.on_answer()
    await session.on_utterance(b"a")

    await session.on_hangup(reason="socket closed")

    assert (await load(rt, iid)).status == "completed", "RECORDS_RECEIVED, from a call that dropped"


async def test_hanging_up_twice_is_harmless(rt, seed):
    """A hang-up and a dropped socket both arrive for the same call."""
    iid, call_id = await place(rt, seed)
    session = session_for(rt, call_id, heard=["No answer needed."], says=["Bye."])
    await session.on_answer()
    await session.on_hangup()
    await session.on_hangup(reason="socket closed")

    assert (await call_row(rt, call_id)).status == "completed"
    assert (await events(rt, iid)).count("call_ended") == 1


async def test_an_unanswered_call_reaches_the_existing_retry_ladder(rt, seed):
    """No new code path for a missed call: it is a no-answer like any other."""
    iid, call_id = await place(rt, seed)

    await miss_call(rt.session_factory, rt.clock, rt.engine, call_id)

    assert (await call_row(rt, call_id)).status == "missed"
    inst = await load(rt, iid)
    assert inst.attempt_count == 1 and inst.wake_reason == "retry"
    assert "call_missed" in await events(rt, iid)


async def test_missing_an_already_answered_call_does_nothing(rt, seed):
    _iid, call_id = await place(rt, seed)
    session = session_for(rt, call_id, heard=["hello"], says=["hi"])
    await session.on_answer()

    await miss_call(rt.session_factory, rt.clock, rt.engine, call_id)
    assert (await call_row(rt, call_id)).status == "active", "they had already picked up"


async def test_answering_a_call_that_is_not_ringing_is_refused(rt, seed):
    import pytest

    _iid, call_id = await place(rt, seed)
    session = session_for(rt, call_id, heard=[], says=[])
    await session.on_answer()

    second = session_for(rt, call_id, heard=[], says=[])
    with pytest.raises(LookupError):
        await second.on_answer()


async def test_a_call_with_no_instance_behind_it_does_not_explode(rt, seed):
    """Placed by hand, or its instance deleted: still ends cleanly."""
    from app.voice.repository import place_call

    async with rt.session_factory() as s, s.begin():
        call = await place_call(
            s, instance_id=None, contact_id=seed.provider_id, goal="g", opening="o",
            to_address="+15550100", now=rt.clock.now(),
        )
        call_id = call.id

    session = session_for(rt, call_id, heard=["hello"], says=["hi"])
    await session.on_answer()
    await session.on_utterance(b"a")
    await session.on_hangup()
    assert (await call_row(rt, call_id)).status == "completed"


async def test_the_brain_is_told_the_words_came_off_a_call(rt, seed):
    """The channel reaches the extraction prompt, which is what switches it into
    treating the text as a transcription rather than as typed."""
    iid, call_id = await place(rt, seed)
    async with rt.session_factory() as s:
        inst = await s.get(type(await load(rt, iid)), iid)
        ctx = await rt.engine._brain_context(s, inst, "call")
    assert ctx["channel"] == "call"

    async with rt.session_factory() as s:
        typed = await rt.engine._brain_context(s, inst)
    assert typed["channel"] == "sms", "the default is still a typed message"
