"""An outreach that gets no reply at all must still move.

The retry ladder used to advance only when an explicit NO_ANSWER signal arrived from
outside. A sent message that nobody ever answers produced no signal, so the instance
sat in `awaiting_reply` with nothing scheduled - stuck, silently, forever. Every
workflow was affected.
"""
from __future__ import annotations

from datetime import timedelta

from tests.helpers import events, load, medical_context, review_tasks, tick


async def start(rt, seed, workflow="medical_records_followup", **ctx):
    context = medical_context(seed) if workflow == "medical_records_followup" else ctx
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, workflow, case_id=seed.case_id, context=context)
        return inst.id


async def test_silence_after_an_attempt_still_advances_the_ladder(rt, seed):
    iid = await start(rt, seed)
    inst = await load(rt, iid)
    assert inst.next_wake_at is not None, "sending and then waiting forever is not a state"
    assert inst.wake_reason == "response_timeout"

    # nobody ever replies: the deadline fires and counts as a no-answer
    rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
    assert [r for _, r in await tick(rt)] == ["executed"]
    inst = await load(rt, iid)
    assert inst.attempt_count == 1 and inst.wake_reason == "retry"
    assert "response_timeout" in await events(rt, iid)

    # and it keeps going on its own until the ladder is spent
    for _ in range(6):
        inst = await load(rt, iid)
        if inst.status != "active" or inst.next_wake_at is None:
            break
        rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
        await tick(rt)

    inst = await load(rt, iid)
    assert inst.status == "blocked", "unanswered outreach must end at a human, not in limbo"
    assert [t.reason for t in await review_tasks(rt, iid)] == ["stalled_no_response"]


async def test_a_reply_supersedes_the_deadline(rt, seed):
    iid = await start(rt, seed)
    deadline = (await load(rt, iid)).next_wake_at

    async with rt.session_factory() as s, s.begin():
        await rt.engine.handle_inbound(s, iid, "records not available for 2 weeks")

    inst = await load(rt, iid)
    assert inst.wake_reason == "dynamic_reschedule"
    assert inst.next_wake_at != deadline, "the reply replaces the deadline"

    # the superseded deadline must not fire later
    rt.clock.set(deadline + timedelta(minutes=1))
    assert await tick(rt) == []


async def test_completion_clears_the_deadline(rt, seed):
    iid = await start(rt, seed)
    async with rt.session_factory() as s, s.begin():
        await rt.engine.handle_inbound(s, iid, "Here are the records, sent by fax")
    inst = await load(rt, iid)
    assert inst.status == "completed" and inst.next_wake_at is None


async def test_deadline_is_declared_per_workflow(rt, seed):
    """It is workflow config, not an engine constant - a check-in waits differently
    from a records chase."""
    from app.workflows.registry import default_registry

    registry = default_registry()
    assert registry.get("medical_records_followup").response_deadline
    assert registry.get("client_checkin").response_deadline

    iid = await start(rt, seed, workflow="client_checkin",
                      target_contact_id=str(seed.client_id), client_contact_id=str(seed.client_id))
    inst = await load(rt, iid)
    assert inst.wake_reason == "response_timeout"
    rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
    assert [r for _, r in await tick(rt)] == ["executed"]
    assert (await load(rt, iid)).attempt_count == 1
