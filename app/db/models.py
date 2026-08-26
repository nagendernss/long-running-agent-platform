"""SQLAlchemy models mirroring migrations/*.sql (the SQL files are the source of truth)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Float, ForeignKey, Identity, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Contact(Base):
    __tablename__ = "contact"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text, default="America/New_York")
    business_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class CaseRecord(Base):
    __tablename__ = "case_record"
    id: Mapped[uuid.UUID] = _uuid_pk()
    client_contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contact.id"))
    matter_type: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class WorkflowInstance(Base):
    __tablename__ = "workflow_instance"
    id: Mapped[uuid.UUID] = _uuid_pk()
    workflow_type: Mapped[str] = mapped_column(Text)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("case_record.id"))
    state: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="active")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_wake_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    wake_reason: Mapped[str | None] = mapped_column(Text)
    wake_token: Mapped[str | None] = mapped_column(Text)
    wake_job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class Event(Base):
    __tablename__ = "event"
    id: Mapped[uuid.UUID] = _uuid_pk()
    # BIGSERIAL in SQL: monotonic tiebreaker so the audit timeline is exact even
    # when several events share a timestamp (virtual clock, same-transaction writes).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), server_default=None)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_instance.id"))
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class EntityFactVersion(Base):
    __tablename__ = "entity_fact_version"
    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    field: Mapped[str] = mapped_column(Text)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("event.id"))
    status: Mapped[str] = mapped_column(Text, default="applied")
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class ReviewTask(Base):
    __tablename__ = "review_task"
    id: Mapped[uuid.UUID] = _uuid_pk()
    instance_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_instance.id"))
    reason: Mapped[str] = mapped_column(Text)
    context_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    suggested_options: Mapped[list[Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="pending")
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


# entity_type -> model, for base-column fallback in the fact resolver
ENTITY_MODELS: dict[str, type[Base]] = {
    "contact": Contact,
    "case_record": CaseRecord,
}
