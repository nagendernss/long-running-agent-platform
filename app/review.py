"""Human-in-the-loop extension point.

This slice only creates review tasks (and blocks the instance) plus a minimal
resolve path. Routing rules, SLAs, assignment and the review UI are deferred and
should hang off this module without touching the Engine.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReviewTask, WorkflowInstance
from app.events import log_event


async def create_review_task(
    session: AsyncSession,
    instance: WorkflowInstance,
    reason: str,
    *,
    now: datetime,
    suggested_options: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> ReviewTask:
    task = ReviewTask(
        id=uuid.uuid4(),
        instance_id=instance.id,
        reason=reason,
        context_snapshot={
            "state": instance.state,
            "attempt_count": instance.attempt_count,
            "context": instance.context,
            **(extra or {}),
        },
        suggested_options=suggested_options or [],
        status="pending",
        created_at=now,
    )
    session.add(task)
    instance.status = "blocked"
    instance.updated_at = now
    await session.flush()
    await log_event(
        session,
        instance.id,
        "escalated",
        {"reason": reason, "review_task_id": str(task.id), "suggested_options": task.suggested_options},
        now=now,
    )
    return task


async def list_pending_review_tasks(session: AsyncSession) -> list[ReviewTask]:
    stmt = select(ReviewTask).where(ReviewTask.status == "pending").order_by(ReviewTask.created_at.asc())
    return list((await session.execute(stmt)).scalars().all())
