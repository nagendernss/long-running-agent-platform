from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models import ReviewTask, WorkflowInstance
from app.events import list_events
from app.runtime import Runtime


async def load(rt: Runtime, instance_id: uuid.UUID) -> WorkflowInstance:
    async with rt.session_factory() as s:
        return await s.get(WorkflowInstance, instance_id)


async def inbound(rt: Runtime, instance_id: uuid.UUID, text: str, channel: str = "sms"):
    async with rt.session_factory() as s, s.begin():
        return await rt.engine.handle_inbound(s, instance_id, text, channel=channel)


async def tick(rt: Runtime):
    async with rt.session_factory() as s, s.begin():
        return await rt.engine.run_due(s)


async def events(rt: Runtime, instance_id: uuid.UUID) -> list[str]:
    async with rt.session_factory() as s:
        return [e.type for e in await list_events(s, instance_id)]


async def review_tasks(rt: Runtime, instance_id: uuid.UUID) -> list[ReviewTask]:
    async with rt.session_factory() as s:
        stmt = select(ReviewTask).where(ReviewTask.instance_id == instance_id).order_by(ReviewTask.created_at)
        return list((await s.execute(stmt)).scalars().all())


def medical_context(seed) -> dict:
    return {
        "target_contact_id": str(seed.provider_id),
        "provider_contact_id": str(seed.provider_id),
        "client_contact_id": str(seed.client_id),
        "provider_channel": "call",
        "client_channel": "sms",
    }


def checkin_context(seed) -> dict:
    return {
        "target_contact_id": str(seed.client_id),
        "client_contact_id": str(seed.client_id),
        "client_channel": "sms",
    }
