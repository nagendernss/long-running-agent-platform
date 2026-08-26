"""Current-value resolution for the versioned fact store.

Anything that needs a contact's phone/email/etc MUST go through `get_entity_field` -
never read the denormalized column directly, it may be stale.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ENTITY_MODELS, EntityFactVersion


async def get_current_fact(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID, field: str
) -> EntityFactVersion | None:
    """Latest *applied* fact version for (entity_type, entity_id, field), or None."""
    stmt = (
        select(EntityFactVersion)
        .where(
            EntityFactVersion.entity_type == entity_type,
            EntityFactVersion.entity_id == entity_id,
            EntityFactVersion.field == field,
            EntityFactVersion.status == "applied",
        )
        .order_by(EntityFactVersion.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_current_value(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID, field: str
) -> str | None:
    fact = await get_current_fact(session, entity_type, entity_id, field)
    return fact.new_value if fact else None


async def get_entity_field(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID, field: str
) -> str | None:
    """Fact-store value if one was ever applied, else the entity's base column."""
    value = await get_current_value(session, entity_type, entity_id, field)
    if value is not None:
        return value
    model = ENTITY_MODELS.get(entity_type)
    if model is None or not hasattr(model, field):
        return None
    row = await session.get(model, entity_id)
    if row is None:
        return None
    base = getattr(row, field)
    return None if base is None else str(base)
