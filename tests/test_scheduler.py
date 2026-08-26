"""Durable scheduling: Procrastinate really fires wakes; stale/duplicate wakes are no-ops."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import WorkflowInstance
from tests.helpers import events, load, medical_context


async def start(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed))
        return inst.id


async def run_worker(rt):
    await rt.procrastinate_app.run_worker_async(wait=False, install_signal_handlers=False)


async def test_procrastinate_worker_executes_due_wake(rt, seed):
    rt.clock.set(datetime.now(timezone.utc))  # Procrastinate compares schedule_at against real DB time
    iid = await start(rt, seed)
    sent_before = len(rt.channel.outbox)

    async with rt.session_factory() as s, s.begin():
        inst = await s.get(WorkflowInstance, iid, with_for_update=True)
        await rt.scheduler.schedule_wake(s, inst, rt.clock.now() - timedelta(seconds=1), reason="durable_test")
        job_id = inst.wake_job_id
    assert job_id is not None
    jobs = list(await rt.procrastinate_app.job_manager.list_jobs_async(id=job_id))
    assert jobs and jobs[0].status == "todo" and jobs[0].lock == f"instance:{iid}"

    await run_worker(rt)

    jobs = list(await rt.procrastinate_app.job_manager.list_jobs_async(id=job_id))
    assert jobs[0].status == "succeeded"
    inst = await load(rt, iid)
    assert inst.next_wake_at is None and inst.wake_token is None and inst.wake_job_id is None
    assert len(rt.channel.outbox) == sent_before + 1
    assert rt.channel.outbox[-1].address == seed.provider_phone
    ev = await events(rt, iid)
    assert ev.count("attempt_started") == 2  # initial + durable wake


async def test_reschedule_cancels_previous_job_and_stale_token_is_noop(rt, seed):
    rt.clock.set(datetime.now(timezone.utc))
    iid = await start(rt, seed)
    async with rt.session_factory() as s, s.begin():
        inst = await s.get(WorkflowInstance, iid, with_for_update=True)
        first_token = await rt.scheduler.schedule_wake(s, inst, rt.clock.now() - timedelta(seconds=2), reason="first")
        first_job = inst.wake_job_id
        await rt.scheduler.schedule_wake(s, inst, rt.clock.now() - timedelta(seconds=1), reason="second")
        second_job = inst.wake_job_id
    assert first_job != second_job

    jobs = {j.id: j.status for j in await rt.procrastinate_app.job_manager.list_jobs_async()}
    assert jobs[first_job] == "cancelled" and jobs[second_job] == "todo"

    # a stale token (e.g. a job that slipped past cancellation) must not execute
    async with rt.session_factory() as s, s.begin():
        assert await rt.engine.execute_wakeup(s, iid, first_token) == "skipped:stale_token"

    sent_before = len(rt.channel.outbox)
    await run_worker(rt)
    assert len(rt.channel.outbox) == sent_before + 1
    inst = await load(rt, iid)
    assert inst.wake_reason is None and inst.next_wake_at is None

    # nothing scheduled anymore -> a late duplicate is a no-op
    async with rt.session_factory() as s, s.begin():
        assert await rt.engine.execute_wakeup(s, iid) == "skipped:nothing_scheduled"
    assert "wake_skipped" in await events(rt, iid)


async def test_blocked_instance_does_not_wake(rt, seed):
    iid = await start(rt, seed)
    async with rt.session_factory() as s, s.begin():
        inst = await s.get(WorkflowInstance, iid, with_for_update=True)
        await rt.scheduler.schedule_wake(s, inst, rt.clock.now(), reason="x")
        inst.status = "blocked"
    async with rt.session_factory() as s, s.begin():
        assert await rt.engine.execute_wakeup(s, iid) == "skipped:status=blocked"
