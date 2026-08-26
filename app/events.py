"""Append-only audit trail helpers."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event


async def log_event(
    session: AsyncSession,
    instance_id: uuid.UUID | None,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    now: datetime,
    idempotency_key: str | None = None,
) -> Event:
    ev = Event(
        id=uuid.uuid4(),
        instance_id=instance_id,
        type=type,
        payload=payload or {},
        idempotency_key=idempotency_key,
        created_at=now,
    )
    session.add(ev)
    await session.flush()
    return ev


async def idempotency_key_exists(session: AsyncSession, key: str) -> bool:
    stmt = select(Event.id).where(Event.idempotency_key == key).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def list_events(session: AsyncSession, instance_id: uuid.UUID) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.instance_id == instance_id)
        .order_by(Event.seq.asc())
    )
    return list((await session.execute(stmt)).scalars().all())
