"""Recovery sweep: make sure no instance is left holding a wake that will never fire.

The write path is already fail-safe by ordering. `schedule_wake` defers the
Procrastinate job *before* the app transaction commits, so:

  * if the defer fails, the whole transaction rolls back and the instance never
    records a wake it does not have;
  * if the defer succeeds but the transaction rolls back, the orphan job fires
    against a token that no longer matches and is a no-op.

What ordering cannot cover is everything that happens *after* a good commit, and
that is what this module is for:

  1. **A worker died mid-job.** The job sits in `doing` forever - Procrastinate
     tracks worker heartbeats, so those are detectable and can be retried.
  2. **The job is gone but the instance still expects it.** A purge, a restore
     from backup, a partial migration, someone editing rows by hand, or a run in
     non-durable mode. The instance row is the source of truth, so an active
     instance that is past due with no live job gets a fresh one.

Run it periodically (the worker registers it on a cron) and on worker start-up.
It is idempotent: re-arming rotates the fencing token, so any job that somehow
survives is invalidated rather than duplicated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select, text

from app.db.models import WorkflowInstance
from app.events import log_event

log = logging.getLogger(__name__)

LIVE_JOB_STATUSES = ("todo", "doing")


@dataclass
class RecoveryReport:
    stalled_retried: list[int] = field(default_factory=list)
    rearmed: list[str] = field(default_factory=list)
    checked: int = 0

    def __bool__(self) -> bool:
        return bool(self.stalled_retried or self.rearmed)

    def summary(self) -> str:
        return (
            f"checked {self.checked} due instance(s); "
            f"retried {len(self.stalled_retried)} stalled job(s); "
            f"re-armed {len(self.rearmed)} lost wake(s)"
        )


async def retry_stalled_jobs(runtime, seconds_since_heartbeat: float = 60.0) -> list[int]:
    """A worker that died mid-job leaves it in `doing`. Procrastinate can spot those
    through worker heartbeats; we put them back on the queue."""
    pq = runtime.procrastinate_app
    if pq is None:
        return []
    retried: list[int] = []
    try:
        stalled = list(await pq.job_manager.get_stalled_jobs(seconds_since_heartbeat=seconds_since_heartbeat))
    except Exception:  # older schema, or the heartbeat table is not there yet
        log.debug("could not query stalled jobs", exc_info=True)
        return []
    for job in stalled:
        try:
            await pq.job_manager.retry_job(job)
            retried.append(job.id)
            log.warning("requeued stalled job %s (%s)", job.id, job.task_name)
        except Exception:
            log.exception("could not requeue stalled job %s", job.id)
    return retried


async def rearm_lost_wakes(runtime, grace: timedelta = timedelta(minutes=5)) -> list[str]:
    """Any active instance whose wake is overdue but has no live job gets a new one.

    `grace` keeps the sweep off wakes that are only just due and are probably being
    picked up by a worker right now.
    """
    engine, scheduler = runtime.engine, runtime.scheduler
    cutoff = runtime.clock.now() - grace
    rearmed: list[str] = []

    async with runtime.session_factory() as session:
        stmt = (
            select(WorkflowInstance.id)
            .where(
                WorkflowInstance.status == "active",
                WorkflowInstance.next_wake_at.is_not(None),
                WorkflowInstance.next_wake_at <= cutoff,
            )
            .order_by(WorkflowInstance.next_wake_at.asc())
        )
        candidates = list((await session.execute(stmt)).scalars().all())
    checked = len(candidates)

    for instance_id in candidates:
        async with runtime.session_factory() as session, session.begin():
            instance = await engine._lock_instance(session, instance_id)
            if instance is None or instance.status != "active" or instance.next_wake_at is None:
                continue
            if await _has_live_job(session, instance.wake_job_id):
                continue
            reason = instance.wake_reason or "recovered"
            await log_event(
                session, instance.id, "wake_recovered",
                {"was_due_at": instance.next_wake_at.isoformat(), "reason": reason,
                 "missing_job_id": instance.wake_job_id},
                now=runtime.clock.now(),
            )
            # Re-arm at the original time (already in the past) so it runs immediately.
            await scheduler.schedule_wake(session, instance, instance.next_wake_at, reason=reason)
            rearmed.append(str(instance.id))
            log.warning("re-armed lost wake for instance %s (was due %s)", instance.id, instance.next_wake_at)
    rearm_lost_wakes.last_checked = checked  # surfaced through RecoveryReport.checked
    return rearmed


async def _has_live_job(session, job_id: int | None) -> bool:
    if job_id is None:
        return False
    row = await session.execute(
        # status is a Postgres enum, so compare as text rather than relying on inference
        text("SELECT 1 FROM procrastinate_jobs WHERE id = :id AND status::text = ANY(:statuses)"),
        {"id": job_id, "statuses": list(LIVE_JOB_STATUSES)},
    )
    return row.scalar_one_or_none() is not None


async def recover(runtime, *, grace: timedelta = timedelta(minutes=5)) -> RecoveryReport:
    report = RecoveryReport()
    report.stalled_retried = await retry_stalled_jobs(runtime)
    report.rearmed = await rearm_lost_wakes(runtime, grace=grace)
    report.checked = getattr(rearm_lost_wakes, "last_checked", 0)
    if report:
        log.warning("wake recovery: %s", report.summary())
    else:
        log.info("wake recovery: nothing to do")
    return report
