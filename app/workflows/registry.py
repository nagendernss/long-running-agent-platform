"""Workflow registry - THE extension point. Adding workflow #3 = one new file + one
`register(...)` line here. Nothing in the Engine, scheduler or fact system changes."""
from __future__ import annotations

from app.workflows.base import WorkflowDefinition


class WorkflowRegistry:
    def __init__(self) -> None:
        self._defs: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        if definition.workflow_type in self._defs:
            raise ValueError(f"workflow already registered: {definition.workflow_type}")
        self._defs[definition.workflow_type] = definition

    def get(self, workflow_type: str) -> WorkflowDefinition:
        try:
            return self._defs[workflow_type]
        except KeyError:
            raise KeyError(f"unknown workflow_type: {workflow_type!r}") from None

    def types(self) -> list[str]:
        return sorted(self._defs)


def default_registry() -> WorkflowRegistry:
    from app.workflows.client_checkin import ClientCheckinWorkflow
    from app.workflows.contact_update import ContactUpdateWorkflow
    from app.workflows.medical_records import MedicalRecordsFollowupWorkflow

    registry = WorkflowRegistry()
    registry.register(MedicalRecordsFollowupWorkflow())
    registry.register(ClientCheckinWorkflow())
    registry.register(ContactUpdateWorkflow())   # <- the whole cost of a new use case
    return registry
