"""Workflow Definition contract.

A WorkflowDefinition is declarative per-use-case config plus domain-signal handlers.
It never talks to the scheduler, channels, DB or review queue directly - it only
calls back through the `WorkflowContext` the Engine hands it. That is what keeps
"add a new workflow" a one-file change.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, ReviewTask, WorkflowInstance
from app.signals import Signal

if TYPE_CHECKING:
    from app.agent_brain import KeywordRule


class WorkflowContext(Protocol):
    """Capabilities the Engine exposes to a workflow definition."""

    session: AsyncSession
    now: datetime

    async def send(
        self, instance: WorkflowInstance, contact_id: uuid.UUID | str, body: str, channel: str = "sms"
    ) -> str: ...

    async def schedule_wake(self, instance: WorkflowInstance, at: datetime, reason: str) -> None: ...

    async def schedule_wake_in(self, instance: WorkflowInstance, duration: str, reason: str) -> None:
        """Wake after `duration` ("14d"), clamped to the target contact's business hours."""
        ...

    async def transition(self, instance: WorkflowInstance, new_state: str) -> None: ...

    async def complete(self, instance: WorkflowInstance, outcome: str) -> None: ...

    async def create_review_task(
        self,
        instance: WorkflowInstance,
        reason: str,
        suggested_options: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ReviewTask: ...

    async def log(self, instance: WorkflowInstance, type: str, payload: dict[str, Any]) -> Event: ...

    async def contact_field(self, contact_id: uuid.UUID | str, field: str) -> str | None: ...

    async def contact_name(self, contact_id: uuid.UUID | str) -> str: ...


class WorkflowDefinition(Protocol):
    workflow_type: str
    initial_state: str
    retry_policy: dict[str, Any]
    domain_signals: list[type[Signal]]  # for a future LLM brain to build its tool schema
    keyword_rules: list["KeywordRule"]  # for the rule-based brain stub

    async def on_start(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None: ...
    async def on_wake(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None: ...
    async def on_enter_state(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None: ...
    async def handle_domain_signal(
        self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext
    ) -> None: ...
    async def on_generic_outcome(
        self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext
    ) -> None: ...
    async def on_review_resolved(
        self, instance: WorkflowInstance, task: ReviewTask, ctx: WorkflowContext
    ) -> None: ...


class BaseWorkflow:
    """Sensible defaults so a definition only overrides what it cares about."""

    workflow_type: ClassVar[str]
    initial_state: ClassVar[str]
    retry_policy: ClassVar[dict[str, Any]] = {}
    domain_signals: ClassVar[list[type[Signal]]] = []
    keyword_rules: ClassVar[list["KeywordRule"]] = []

    async def on_start(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        await self.on_wake(instance, ctx)

    async def on_wake(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        return None

    async def on_enter_state(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        return None

    async def handle_domain_signal(
        self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext
    ) -> None:
        await ctx.log(instance, "unhandled_domain_signal", {"signal": signal.model_dump()})

    async def on_generic_outcome(
        self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext
    ) -> None:
        return None

    async def on_review_resolved(
        self, instance: WorkflowInstance, task: ReviewTask, ctx: WorkflowContext
    ) -> None:
        """Default staff resolutions: {"action": "retry"} restarts outreach now,
        {"action": "close"} completes the instance. Workflows extend for domain cases."""
        action = (task.resolution or {}).get("action")
        if action == "retry":
            instance.attempt_count = 0
            await ctx.schedule_wake(instance, ctx.now, reason="resume_after_review")
        elif action == "close":
            await ctx.complete(instance, outcome="closed_by_staff")
