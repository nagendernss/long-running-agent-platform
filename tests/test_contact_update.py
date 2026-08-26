"""Workflow #3, shipped: "send one update to one contact".

Its whole implementation is one file plus one registry line. These tests assert it
inherits the platform - retries, business-hours clamping, durable wakes, fact
write-back, channel switching, escalation, the API and the dashboard - for free.
"""
from __future__ import annotations

from datetime import timedelta

from app.db.facts import get_entity_field
from tests.helpers import events, inbound, load, review_tasks, tick


def ctx(seed, **over):
    base = {"target_contact_id": str(seed.client_id), "message": "Your hearing moved to March 3rd.", "channel": "sms"}
    return {**base, **over}


async def start(rt, seed, **over):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, "contact_update", case_id=seed.case_id, context=ctx(seed, **over))
        return inst.id


async def test_sends_once_and_completes_when_acknowledged(rt, seed):
    iid = await start(rt, seed)
    sent = rt.channel.outbox[-1]
    assert sent.address == seed.client_phone and sent.body == "Your hearing moved to March 3rd."
    assert (await load(rt, iid)).state == "awaiting_ack"

    await inbound(rt, iid, "Got it, thanks")
    inst = await load(rt, iid)
    assert inst.status == "completed" and inst.state == "completed"
    assert "completed" in await events(rt, iid)


async def test_fire_and_forget_completes_on_send(rt, seed):
    iid = await start(rt, seed, require_ack=False)
    inst = await load(rt, iid)
    assert inst.status == "completed" and inst.next_wake_at is None
    assert len(rt.channel.outbox) >= 1


async def test_inherits_the_retry_ladder_and_escalation(rt, seed):
    """No retry code in the workflow - the ladder comes from its declared policy."""
    iid = await start(rt, seed)
    for attempt, days in enumerate(["1d", "3d"], start=1):
        await inbound(rt, iid, "no answer")
        inst = await load(rt, iid)
        assert inst.attempt_count == attempt and inst.wake_reason == "retry"
        assert inst.next_wake_at - rt.clock.now() >= timedelta(days=int(days[:-1]))
        rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
        assert [r for _, r in await tick(rt)] == ["executed"]

    await inbound(rt, iid, "no answer")  # ladder exhausted
    inst = await load(rt, iid)
    assert inst.status == "blocked" and inst.next_wake_at is None, "handing off stops the clock"
    assert [t.reason for t in await review_tasks(rt, iid)] == ["stalled_no_response"]


async def test_inherits_fact_write_back_and_channel_switching(rt, seed):
    """"wrong number" and "email me instead" work with zero workflow code."""
    iid = await start(rt, seed)
    await inbound(rt, iid, "wrong number, use 555-0142")
    async with rt.session_factory() as s:
        assert await get_entity_field(s, "contact", seed.client_id, "phone") == "5550142"

    await inbound(rt, iid, "we do not take these over the phone, email me at jane.new@example.com")
    async with rt.session_factory() as s:
        assert await get_entity_field(s, "contact", seed.client_id, "preferred_channel") == "email"

    rt.channel.outbox.clear()
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.scheduler.schedule_wake(s, inst, rt.clock.now(), reason="retry")
    assert [r for _, r in await tick(rt)] == ["executed"]
    sent = rt.channel.outbox[-1]
    assert sent.channel == "email" and sent.address == "jane.new@example.com"


async def test_reminder_schedules_one_nudge(rt, seed):
    iid = await start(rt, seed, reminder="3d")
    inst = await load(rt, iid)
    assert inst.wake_reason == "reminder" and inst.next_wake_at - rt.clock.now() >= timedelta(days=3)

    rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
    assert [r for _, r in await tick(rt)] == ["executed"]
    inst = await load(rt, iid)
    assert inst.context["reminded"] is True
    assert inst.wake_reason == "response_timeout", "one nudge, then the deadline takes over"


async def test_it_shows_up_in_the_api_with_no_api_changes(rt, seed):
    from httpx import ASGITransport, AsyncClient

    from app.api.main import app

    iid = await start(rt, seed)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        catalog = (await c.get("/api/workflows")).json()
        assert catalog["contact_update"]["initial_state"] == "awaiting_ack"
        assert catalog["contact_update"]["domain_signals"] == ["ACKNOWLEDGED"]

        listed = (await c.get("/api/instances", params={"workflow_type": "contact_update"})).json()
        assert [i["id"] for i in listed] == [str(iid)]

        page = (await c.get(f"/instances/{iid}")).text
        assert "contact_update" in page and "How it played out" in page
