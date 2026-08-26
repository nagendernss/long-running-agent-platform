"""Workflow types stored as rows, and how a row becomes a runnable definition."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import WorkflowTypeRow
from app.workflows.base import WorkflowDefinition
from app.workflows.templates.outreach import TEMPLATES


def build_definition(name: str, template: str, spec: dict[str, Any]) -> WorkflowDefinition:
    """Validate a stored spec against its template and construct the definition.

    Raises KeyError for an unknown template and pydantic's ValidationError for a spec
    the template cannot accept. Both are worth failing loudly: a broken row would
    otherwise surface as a workflow that behaves subtly wrong.
    """
    try:
        template_cls = TEMPLATES[template]
    except KeyError:
        raise KeyError(f"unknown workflow template: {template!r}") from None
    return template_cls(name, template_cls.spec_model(**spec))


async def list_types(session) -> list[WorkflowTypeRow]:
    stmt = select(WorkflowTypeRow).order_by(WorkflowTypeRow.name)
    return list((await session.execute(stmt)).scalars().all())


async def load_definitions(session) -> dict[str, WorkflowDefinition]:
    return {row.name: build_definition(row.name, row.template, row.spec) for row in await list_types(session)}


async def upsert_type(
    session, *, name: str, template: str, spec: dict[str, Any], description: str | None, now: datetime
) -> WorkflowTypeRow:
    build_definition(name, template, spec)  # refuse to store something that cannot run
    stmt = (
        insert(WorkflowTypeRow)
        .values(
            name=name, template=template, spec=spec, description=description,
            created_at=now, updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[WorkflowTypeRow.name],
            set_={"template": template, "spec": spec, "description": description, "updated_at": now},
        )
        .returning(WorkflowTypeRow)
    )
    return (await session.execute(stmt)).scalar_one()


async def types_version(session) -> tuple[int, datetime | None]:
    """Cheap staleness probe for the registry: how many types exist and when one last
    changed. Two aggregates on a small table, run at most twice a minute per process."""
    row = (
        await session.execute(select(func.count(WorkflowTypeRow.name), func.max(WorkflowTypeRow.updated_at)))
    ).one()
    return int(row[0]), row[1]
