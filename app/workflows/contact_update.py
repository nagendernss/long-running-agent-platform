"""Reference workflow #3: send one update to one contact, and make sure it landed.

The smallest useful thing this platform can run, and the floor for "how much code is
a new use case". Everything below is either the message to send or when to give up -
there is no scheduling, retry, fact or escalation logic here, because the Engine owns
all of that generically.

Instance context contract:
    target_contact_id - who to tell (also what the Engine clamps scheduling against)
    message           - the update text
    channel           - call | sms | email   (default sms; a preferred_channel fact wins)
    require_ack       - bool, default True. False = fire-and-forget, completes on send.
    reminder          - optional duration ("3d") to nudge once before the retry ladder
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from app.agent_brain import KeywordRule, rule
from app.db.models import WorkflowInstance
from app.signals import Signal
from app.workflows.base import BaseWorkflow, WorkflowContext


class Acknowledged(Signal):
    """The contact confirmed they received the update - "got it", "thanks", "noted",
    "will do". Any substantive reply that is not a question also counts."""

    type: Literal["ACKNOWLEDGED"] = "ACKNOWLEDGED"


class ContactUpdateWorkflow(BaseWorkflow):
    workflow_type: ClassVar[str] = "contact_update"
    initial_state: ClassVar[str] = "awaiting_ack"
    retry_policy: ClassVar[dict[str, Any]] = {"no_answer": {"schedule": ["1d", "3d"]}}
    response_deadline: ClassVar[str | None] = "2d"
    domain_signals: ClassVar[list[type[Signal]]] = [Acknowledged]
    keyword_rules: ClassVar[list[KeywordRule]] = [
        rule(
            "acknowledged",
            r"\b(got it|received|thanks|thank you|noted|acknowledged|will do|understood|ok(ay)?)\b",
            lambda m, t, c: Acknowledged(confidence=0.85, evidence=m.group(0)),
        ),
    ]

    async def on_wake(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        c = instance.context
        await ctx.send(instance, c["target_contact_id"], c["message"], channel=c.get("channel", "sms"))
        if not c.get("require_ack", True):
            await ctx.complete(instance, outcome="delivered")
            return
        await ctx.transition(instance, "awaiting_ack")
        if c.get("reminder") and not c.get("reminded"):
            instance.context = {**c, "reminded": True}
            await ctx.schedule_wake_in(instance, c["reminder"], reason="reminder")

    async def handle_domain_signal(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        if isinstance(signal, Acknowledged):
            await ctx.complete(instance, outcome="acknowledged")
        else:
            await super().handle_domain_signal(instance, signal, ctx)
