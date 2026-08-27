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
    assert call is not None and call.status == "queued", "placed, not yet offered to anyone"
    assert call.to_address == seed.provider_phone
    assert call.opening.startswith("Hi Mercy Hospital Records")
    assert "records" in call.goal.lower()

    inst = await load(rt, iid)
    assert inst.status == "active" and inst.wake_reason == "response_timeout"


async def test_the_goal_comes_from_what_the_workflow_already_declares(rt):
    goal = build_goal(rt.registry.get("medical_records_followup"))
    assert "RECORDS_RECEIVED" in goal and "AUTH_REQUIRED" in goal
    assert "authorization" in goal.lower(), "the signal docstrings carry the domain"
    assert "do not raise money yourself" in goal.lower(), "it records a fee, it does not go asking"


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


async def test_a_call_placed_by_a_server_built_runtime_knows_what_it_is_chasing(settings, schema):
    """The bug this catches: build_runtime took `registry=None`, handed that straight
    to VoiceChannel, and only then built the default registry. Every call the real
    server placed went out with the generic fallback goal - "find out what they can
    tell us" - with no mention of records, authorizations or refusals. Every test
    passed a registry explicitly, so every test saw the right goal.

    So this one builds the runtime the way scripts/serve.py does: no registry.
    """
    import uuid as _uuid
    from dataclasses import replace

    from app.channels import MockChannel
    from app.clock import FakeClock
    from app.db.models import CaseRecord, Contact
    from app.runtime import build_runtime

    # voice on, and no registry argument - exactly how scripts/serve.py builds it
    rt = await build_runtime(replace(settings, voice_calls=True), clock=FakeClock(),
                             channel=MockChannel(), durable=False)
    try:
        async with rt.session_factory() as s, s.begin():
            provider = Contact(id=_uuid.uuid4(), name="Mercy Hospital Records", role="provider",
                               phone="+15550100", created_at=rt.clock.now())
            client = Contact(id=_uuid.uuid4(), name="Jane Okafor", role="client",
                             phone="+15550001", created_at=rt.clock.now())
            s.add_all([provider, client])
            await s.flush()
            case = CaseRecord(id=_uuid.uuid4(), client_contact_id=client.id,
                              matter_type="personal_injury", created_at=rt.clock.now())
            s.add(case)
            await s.flush()
            inst = await rt.engine.start_instance(
                s, "medical_records_followup", case_id=case.id,
                context={"target_contact_id": str(provider.id), "provider_contact_id": str(provider.id),
                         "client_contact_id": str(client.id), "provider_channel": "call",
                         "client_channel": "sms"},
            )
            instance_id = inst.id

        async with rt.session_factory() as s:
            call = (await s.execute(
                select(CallRow).where(CallRow.instance_id == instance_id)
            )).scalars().first()

        assert call is not None, "the initial attempt places a call"
        assert "RECORDS_RECEIVED" in call.goal, "the agent must know what it is listening for"
        assert "AUTH_REQUIRED" in call.goal and "REQUEST_DENIED" in call.goal
        assert "medical records" in call.goal, "and what the whole call is for"
    finally:
        await rt.close()
