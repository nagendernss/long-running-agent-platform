"""Write-back Resolver: turns an ENTITY_UPDATE signal into a versioned fact row.

Generic over entity types: the Field Registry decides which (entity_type, field)
pairs are writable and the confidence threshold for auto-apply. No workflow-specific
code anywhere in here.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.facts import get_entity_field
from app.db.models import EntityFactVersion
from app.field_registry import FieldRegistry
from app.signals import EntityUpdate


class WriteBackResolver:
    def __init__(self, registry: FieldRegistry):
        self.registry = registry

    async def apply(
        self,
        session: AsyncSession,
        signal: EntityUpdate,
        *,
        source_event_id: uuid.UUID | None,
        now: datetime,
    ) -> EntityFactVersion:
        """Record the fact. status = applied | proposed (below threshold) | rejected (unregistered field)."""
        entity_id = uuid.UUID(str(signal.entity_id))
        policy = self.registry.get(signal.entity_type, signal.field)
        if policy is None:
            status = "rejected"
        elif signal.confidence >= policy.auto_apply_threshold:
            status = "applied"
        else:
            status = "proposed"

        old_value = await get_entity_field(session, signal.entity_type, entity_id, signal.field)
        fact = EntityFactVersion(
            id=uuid.uuid4(),
            entity_type=signal.entity_type,
            entity_id=entity_id,
            field=signal.field,
            old_value=old_value,
            new_value=signal.new_value,
            confidence=signal.confidence,
            source_event_id=source_event_id,
            status=status,
            created_at=now,
        )
        session.add(fact)
        await session.flush()
        return fact
