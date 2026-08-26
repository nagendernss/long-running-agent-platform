"""End-to-end scenario for the client check-in workflow (plan Phase 9, step 8)."""
from __future__ import annotations

from datetime import timedelta

from app.db.models import Contact
from app.scheduling.constraints import apply_scheduling_constraints
from tests.helpers import checkin_context, inbound, load, review_tasks, tick


async def start(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, "client_checkin", case_id=seed.case_id, context=checkin_context(seed))
        return inst.id


async def test_checkin_cadence(rt, seed):
    iid = await start(rt, seed)
    assert rt.channel.outbox[-1].address == seed.client_phone and "check-in" in rt.channel.outbox[-1].body
    assert (await load(rt, iid)).state == "awaiting_reply"

    await inbound(rt, iid, "Doing fine, nothing new")
    inst = await load(rt, iid)
    async with rt.session_factory() as s:
        client = await s.get(Contact, seed.client_id)
    assert inst.state == "scheduled" and inst.wake_reason == "cadence"
    assert inst.next_wake_at == apply_scheduling_constraints(rt.clock.now() + timedelta(days=14), client)

    rt.clock.advance(timedelta(days=15))
    assert [r for _, r in await tick(rt)] == ["executed"]
    assert (await load(rt, iid)).state == "awaiting_reply"
    assert sum(1 for m in rt.channel.outbox if "check-in" in m.body) == 2


async def test_checkin_reschedule_and_no_answer_policy(rt, seed):
    iid = await start(rt, seed)
    await inbound(rt, iid, "busy, call me in 3 days")
    inst = await load(rt, iid)
    assert inst.state == "scheduled" and inst.wake_reason == "dynamic_reschedule"
    assert inst.next_wake_at - rt.clock.now() >= timedelta(days=3)

    rt.clock.advance(timedelta(days=4))
    await tick(rt)
    await inbound(rt, iid, "")  # empty transcript == no answer
    assert (await load(rt, iid)).attempt_count == 1
    await inbound(rt, iid, "no answer")
    assert (await load(rt, iid)).attempt_count == 2
    await inbound(rt, iid, "no answer")  # policy ["1d","3d"] exhausted
    inst = await load(rt, iid)
    assert inst.status == "blocked"
    assert [t.reason for t in await review_tasks(rt, iid)] == ["stalled_no_response"]


async def test_concerning_reply_flags_for_human(rt, seed):
    iid = await start(rt, seed)
    sigs = await inbound(rt, iid, "honestly the pain is getting worse and I can't sleep")
    # the signal is now the template's shared FLAGGED rather than a per-workflow name;
    # the review reason still carries this type's own wording (client_flag:*)
    assert [s.type for s in sigs] == ["FLAGGED"]
    inst = await load(rt, iid)
    assert inst.status == "blocked"
    tasks = await review_tasks(rt, iid)
    assert len(tasks) == 1 and tasks[0].reason.startswith("client_flag:")
    assert "reach out to you directly" in rt.channel.outbox[-1].body
