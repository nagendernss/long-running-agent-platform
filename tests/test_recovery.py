"""Scaling out: a wake must not be lost.

A wake lives in two places written on two connections - the instance row (intent)
and a Procrastinate job (the timer). Ordering makes the write path fail-safe, but
nothing protects what happens after a good commit: a worker dying mid-job, a purge,
a restore, a partial migration. This is the sweep that catches those.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from app.scheduling.recovery import rearm_lost_wakes, recover
from tests.helpers import events, load, medical_context, tick


async def start(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        return inst.id


async def test_a_wake_whose_job_vanished_is_rearmed_and_runs(rt, seed):
    iid = await start(rt, seed)
    inst = await load(rt, iid)
    job_id = inst.wake_job_id
    assert job_id is not None, "durable mode should have deferred a job"

    # the timer disappears - a purge, a restore, a hand-edited row
    async with rt.db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM procrastinate_jobs WHERE id = :id"), {"id": job_id})

    rt.clock.set(inst.next_wake_at + timedelta(minutes=10))
    report = await recover(rt)
    assert str(iid) in report.rearmed
    assert "wake_recovered" in await events(rt, iid)

    inst = await load(rt, iid)
    assert inst.wake_job_id is not None and inst.wake_job_id != job_id, "a fresh job was issued"
    assert [r for _, r in await tick(rt)] == ["executed"], "and the wake actually runs"


async def test_the_sweep_leaves_healthy_instances_alone(rt, seed):
    iid = await start(rt, seed)
    before = await load(rt, iid)
    rt.clock.set(before.next_wake_at + timedelta(minutes=10))

    report = await recover(rt)
    after = await load(rt, iid)
    assert report.rearmed == [], "the job is still there; nothing to fix"
    assert after.wake_token == before.wake_token, "an untouched instance keeps its fencing token"
    assert "wake_recovered" not in await events(rt, iid)


async def test_the_grace_period_keeps_the_sweep_off_fresh_wakes(rt, seed):
    """A wake that just came due is probably being picked up right now."""
    iid = await start(rt, seed)
    inst = await load(rt, iid)
    async with rt.db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM procrastinate_jobs WHERE id = :id"), {"id": inst.wake_job_id})

    rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
    assert await rearm_lost_wakes(rt, grace=timedelta(minutes=5)) == []

    rt.clock.set(inst.next_wake_at + timedelta(minutes=30))
    assert await rearm_lost_wakes(rt, grace=timedelta(minutes=5)) == [str(iid)]


async def test_recovery_ignores_instances_that_are_not_waiting(rt, seed):
    """Blocked, paused and completed instances have no timer by design."""
    iid = await start(rt, seed)
    inst = await load(rt, iid)
    due = inst.next_wake_at
    async with rt.session_factory() as s, s.begin():
        locked = await rt.engine._lock_instance(s, iid)
        locked.status = "blocked"
    async with rt.db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM procrastinate_jobs WHERE id = :id"), {"id": inst.wake_job_id})

    rt.clock.set(due + timedelta(hours=1))
    assert await rearm_lost_wakes(rt) == []


async def test_rearming_is_idempotent(rt, seed):
    """Two sweeps racing must not leave two live timers on one instance."""
    iid = await start(rt, seed)
    inst = await load(rt, iid)
    async with rt.db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM procrastinate_jobs WHERE id = :id"), {"id": inst.wake_job_id})
    rt.clock.set(inst.next_wake_at + timedelta(hours=1))

    first = await rearm_lost_wakes(rt)
    second = await rearm_lost_wakes(rt)
    assert first == [str(iid)] and second == [], "the second pass sees a healthy instance"

    async with rt.db_engine.begin() as conn:
        live = (await conn.execute(
            text("SELECT count(*) FROM procrastinate_jobs WHERE status::text = ANY(ARRAY['todo','doing']) "
                 "AND args->>'instance_id' = :iid"), {"iid": str(iid)},
        )).scalar()
    assert live == 1


async def test_the_sweep_is_registered_to_run_on_its_own(rt):
    """It is worthless if nothing calls it: the worker registers it on a cron."""
    from app.scheduling.scheduler import RECOVERY_CRON, RECOVERY_TASK_NAME

    assert RECOVERY_TASK_NAME in rt.procrastinate_app.tasks
    periodic = {p.task.name: p.cron for p in rt.procrastinate_app.periodic_registry.periodic_tasks.values()}
    assert periodic.get(RECOVERY_TASK_NAME) == RECOVERY_CRON
