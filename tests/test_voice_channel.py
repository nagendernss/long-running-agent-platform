"""Placing a call must not hold the engine's transaction open for the conversation."""
from __future__ import annotations

from sqlalchemy import select

from app.channels.voice import VoiceChannel, build_goal
from app.db.models import CallRow
from tests.helpers import load, medical_context


async def use_voice(rt):
    rt.engine.channel = VoiceChannel(rt.channel, rt.clock, registry=rt.registry)


async def start_medical(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        return inst.id


async def latest_call(rt, instance_id=None) -> CallRow | None:
    async with rt.session_factory() as s:
        stmt = select(CallRow).order_by(CallRow.created_at.desc()).limit(1)
        if instance_id:
            stmt = select(CallRow).where(CallRow.instance_id == instance_id).order_by(CallRow.created_at.desc()).limit(1)
        return (await s.execute(stmt)).scalar_one_or_none()


async def test_sending_on_the_call_channel_places_a_call_and_returns(rt, seed):
    """A call lasts minutes; an engine transaction must not. The attempt finishes
    immediately and the conversation runs on its own."""
    await use_voice(rt)
    iid = await start_medical(rt, seed)

    call = await latest_call(rt, iid)
    assert call is not None and call.status == "ringing"
    assert call.to_address == seed.provider_phone
    assert call.opening.startswith("Hi Mercy Hospital Records")
    assert "records" in call.goal.lower()

    inst = await load(rt, iid)
    assert inst.status == "active" and inst.wake_reason == "response_timeout"


async def test_the_goal_comes_from_what_the_workflow_already_declares(rt):
    goal = build_goal(rt.registry.get("medical_records_followup"))
    assert "RECORDS_RECEIVED" in goal and "AUTH_REQUIRED" in goal
    assert "authorization" in goal.lower(), "the signal docstrings carry the domain"
    assert "fee" in goal.lower(), "and the standing instruction covers requirements"


async def test_a_second_call_is_not_placed_over_a_live_one(rt, seed):
    await use_voice(rt)
    iid = await start_medical(rt, seed)
    first = await latest_call(rt, iid)

    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.scheduler.schedule_wake(s, inst, rt.clock.now(), reason="retry")
    async with rt.session_factory() as s, s.begin():
        await rt.engine.run_due(s)

    async with rt.session_factory() as s:
        calls = list((await s.execute(select(CallRow).where(CallRow.instance_id == iid))).scalars().all())
    assert [c.id for c in calls] == [first.id], "the line is busy; no second call"


async def test_other_channels_are_untouched(rt, seed):
    """The client is texted while the provider is called - same workflow, one wake."""
    await use_voice(rt)
    iid = await start_medical(rt, seed)
    rt.channel.outbox.clear()

    async with rt.session_factory() as s, s.begin():
        await rt.engine.handle_inbound(s, iid, "no answer")

    texted = [m for m in rt.channel.outbox if m.channel == "sms"]
    assert texted and texted[-1].address == seed.client_phone
    assert not [m for m in rt.channel.outbox if m.channel == "call"], "calls do not go to the mock"
