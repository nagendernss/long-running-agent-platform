"""Workflow registry - THE extension point.

It serves two kinds of workflow side by side:

* **code-defined** - a Python class, for workflows with real branching (several
  parties, per-signal side effects). `medical_records_followup` is one.
* **template-defined** - a row in `workflow_type` naming a template plus a spec.
  These are created from the builder UI and need no deploy.

`get()` is deliberately synchronous: it sits on every hot path. Freshness is a
separate, explicit concern - `ensure_fresh()` re-reads the table at most once per TTL,
and `reload()` is called straight after a mutation so the builder feels instant.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.workflows.base import WorkflowDefinition

log = logging.getLogger(__name__)

DEFAULT_TTL = timedelta(seconds=30)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._code: dict[str, WorkflowDefinition] = {}
        self._db: dict[str, WorkflowDefinition] = {}
        self._checked_at: datetime | None = None
        self._version: tuple[int, datetime | None] | None = None

    # -- code-defined ---------------------------------------------------------------
    def register(self, definition: WorkflowDefinition) -> None:
        if definition.workflow_type in self._code:
            raise ValueError(f"workflow already registered: {definition.workflow_type}")
        self._code[definition.workflow_type] = definition

    def is_code_defined(self, workflow_type: str) -> bool:
        return workflow_type in self._code

    # -- lookup ---------------------------------------------------------------------
    def get(self, workflow_type: str) -> WorkflowDefinition:
        definition = self._code.get(workflow_type) or self._db.get(workflow_type)
        if definition is None:
            raise KeyError(f"unknown workflow_type: {workflow_type!r}")
        return definition

    def types(self) -> list[str]:
        return sorted({*self._code, *self._db})

    # -- freshness ------------------------------------------------------------------
    async def reload(self, session) -> None:
        from app.workflows.types import load_definitions, types_version

        self._db = await load_definitions(session)
        self._version = await types_version(session)
        self._checked_at = datetime.now(timezone.utc)
        log.debug("workflow registry reloaded: %s", sorted(self._db))

    async def reload_from(self, session_factory) -> None:
        async with session_factory() as session:
            await self.reload(session)

    async def ensure_fresh(self, session, ttl: timedelta = DEFAULT_TTL) -> None:
        """Re-read only when the TTL has expired *and* the table actually changed.

        Uses wall-clock time rather than the injected clock on purpose: this is cache
        maintenance, not workflow behaviour, and a test that time-travels a fortnight
        should not trigger a reload.
        """
        now = datetime.now(timezone.utc)
        if self._checked_at is not None and now - self._checked_at < ttl:
            return
        from app.workflows.types import types_version

        self._checked_at = now
        version = await types_version(session)
        if version != self._version:
            await self.reload(session)


def default_registry() -> WorkflowRegistry:
    """Code-defined workflows only. Template-defined ones arrive via `reload()`.

    Adding a workflow with real branching means one file and one line here. Adding one
    of an existing shape means filling in a form - see app/workflows/templates/.
    """
    from app.workflows.medical_records import MedicalRecordsFollowupWorkflow

    registry = WorkflowRegistry()
    registry.register(MedicalRecordsFollowupWorkflow())
    return registry
