"""Durable scheduling on top of Procrastinate (Postgres-backed job queue).

`Scheduler.schedule_wake` is the single choke point: policy retries, dynamic
reschedules, cadence, manual overrides and review resolutions all go through it.

Why both `workflow_instance.next_wake_at` AND a Procrastinate job?
  * The instance row is the source of truth (visible to the dashboard, the demo's
    virtual-clock poller, and any recovery sweep).
  * The Procrastinate job is the durable timer that actually fires in production.
  * A fencing `wake_token` ties the two together: a job only executes if it carries
    the token currently stored on the instance, so a stale job left behind by a
    reschedule (or one that fires after the poller already ran it) is a no-op.

Locking: Procrastinate takes its own row lock on the job, and every wake executes
under `SELECT ... FOR UPDATE` on the instance row, so we do not hand-roll
`locked_until`/`locked_by`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

import procrastinate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.db.models import WorkflowInstance
from app.events import log_event

log = logging.getLogger(__name__)

TASK_NAME = "hc:execute_instance_wakeup"
RECOVERY_TASK_NAME = "hc:recover_lost_wakes"
RECOVERY_CRON = "*/5 * * * *"   # cheap: two indexed queries when nothing is wrong
CALLS_TASK_NAME = "hc:sweep_calls"
CALLS_CRON = "* * * * *"        # every minute: a queue must drain on its own
CLEANUP_TASK_NAME = "hc:prune_finished_jobs"
CLEANUP_CRON = "17 3 * * *"     # nightly, off the hour to avoid every cron firing together
JOB_RETENTION_HOURS = 24 * 30   # a month of finished jobs is plenty; `event` is the audit trail


async def execute_instance_wakeup(instance_id: str, wake_token: str) -> None:
    """The one Procrastinate task. Delegates to the Engine so the worker path and
    the poller/demo path run identical code."""
    from app.runtime import get_runtime  # late import: avoids engine<->scheduler cycle

    result = await get_runtime().engine.run_wakeup(uuid.UUID(instance_id), wake_token)
    log.info("wakeup %s -> %s", instance_id, result)


async def sweep_calls(timestamp: int | None = None) -> None:
    """Keep the phone queue moving. Calls are answered one at a time, so a call nobody
    picks up has to ring out on its own - otherwise it sits at the front forever and
    every instance queued behind it is never attempted."""
    from app.runtime import get_runtime
    from app.voice.queue import sweep_calls as sweep

    runtime = get_runtime()
    if runtime.voice is None:
        return
    result = await sweep(runtime.session_factory, runtime.clock, runtime.engine)
    if result.get("timed_out"):
        log.info("call sweep: %s", result)


async def prune_finished_jobs(timestamp: int | None = None) -> None:
    """Finished jobs and their status events accumulate forever otherwise: at a
    thousand outreach attempts a day that is millions of rows a year of pure
    bookkeeping. Never touches our own `event` table, which is the audit trail
    people actually read."""
    from app.runtime import get_runtime

    manager = get_runtime().procrastinate_app.job_manager
    await manager.delete_old_jobs(nb_hours=JOB_RETENTION_HOURS, include_cancelled=True)
    log.info("pruned procrastinate jobs finished more than %sh ago", JOB_RETENTION_HOURS)


async def recover_lost_wakes(timestamp: int | None = None) -> None:
    """Periodic safety net. A wake lives in two places - the instance row (intent) and
    a Procrastinate job (the timer) - written on separate connections, so anything that
    destroys one without the other leaves an instance waiting on a timer that will
    never fire. This finds those and re-arms them."""
    from app.runtime import get_runtime
    from app.scheduling.recovery import recover

    report = await recover(get_runtime())
    log.info("recovery sweep: %s", report.summary())


def make_procrastinate_app(psycopg_url: str) -> procrastinate.App:
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=psycopg_url))
    # Registered per app instance: procrastinate Blueprints are mutated by
    # add_tasks_from, so a module-level blueprint cannot be reused safely.
    app.task(name=TASK_NAME)(execute_instance_wakeup)
    app.periodic(cron=RECOVERY_CRON, queueing_lock="hc:recovery")(
        app.task(name=RECOVERY_TASK_NAME, pass_context=False)(recover_lost_wakes)
    )
    app.periodic(cron=CALLS_CRON, queueing_lock="hc:calls")(
        app.task(name=CALLS_TASK_NAME, pass_context=False)(sweep_calls)
    )
    app.periodic(cron=CLEANUP_CRON, queueing_lock="hc:cleanup")(
        app.task(name=CLEANUP_TASK_NAME, pass_context=False)(prune_finished_jobs)
    )
    return app


class Scheduler:
    def __init__(self, clock: Clock, procrastinate_app: procrastinate.App | None = None):
        self.clock = clock
        self.procrastinate_app = procrastinate_app
        self.durable = False  # flipped on by the runtime once the Procrastinate app is open

    # -- the choke point -----------------------------------------------------------
    async def schedule_wake(
        self, session: AsyncSession, instance: WorkflowInstance, at: datetime, reason: str
    ) -> str:
        now = self.clock.now()
        await self._cancel_pending_job(instance)
        token = uuid.uuid4().hex
        instance.next_wake_at = at
        instance.wake_reason = reason
        instance.wake_token = token
        instance.updated_at = now
        instance.wake_job_id = await self._defer_job(instance, at, token)
        await session.flush()
        await log_event(
            session,
            instance.id,
            "wake_scheduled",
            {
                "at": at.isoformat(),
                "reason": reason,
                "wake_token": token,
                "job_id": instance.wake_job_id,
                "durable": self.durable,
            },
            now=now,
        )
        return token

    async def clear_wake(self, instance: WorkflowInstance) -> None:
        await self._cancel_pending_job(instance)
        instance.next_wake_at = None
        instance.wake_reason = None
        instance.wake_token = None
        instance.wake_job_id = None

    async def due_instance_ids(self, session: AsyncSession, now: datetime | None = None) -> list[uuid.UUID]:
        now = now or self.clock.now()
        stmt = (
            select(WorkflowInstance.id)
            .where(WorkflowInstance.status == "active", WorkflowInstance.next_wake_at <= now)
            .order_by(WorkflowInstance.next_wake_at.asc())
        )
        return list((await session.execute(stmt)).scalars().all())

    # -- procrastinate plumbing ----------------------------------------------------
    async def _defer_job(self, instance: WorkflowInstance, at: datetime, token: str) -> int | None:
        if not self.durable or self.procrastinate_app is None:
            return None
        task = self.procrastinate_app.tasks[TASK_NAME]
        return await task.configure(
            schedule_at=at,
            lock=f"instance:{instance.id}",  # serialize jobs per instance
        ).defer_async(instance_id=str(instance.id), wake_token=token)

    async def _cancel_pending_job(self, instance: WorkflowInstance) -> None:
        if not self.durable or self.procrastinate_app is None or instance.wake_job_id is None:
            return
        try:
            await self.procrastinate_app.job_manager.cancel_job_by_id_async(instance.wake_job_id)
        except Exception:  # job may already be running/finished; the fencing token covers that
            log.debug("could not cancel job %s", instance.wake_job_id, exc_info=True)
        instance.wake_job_id = None
