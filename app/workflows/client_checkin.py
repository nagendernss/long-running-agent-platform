"""Reference workflow #2: recurring client check-in.

States: scheduled -> awaiting_reply -> scheduled (every `cadence`).
Concerning content in a reply => ClientFlag => review task for staff.

Instance context contract:
    target_contact_id / client_contact_id - the client
    client_channel                        - call | sms | email (default sms)
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from app.agent_brain import KeywordRule, rule
from app.db.models import WorkflowInstance
from app.signals import Signal
from app.workflows.base import BaseWorkflow, WorkflowContext


class ClientFlag(Signal):
    """The client reports something a person must see: worsening injury or pain, a
    hospital or surgery, mental-health distress, or dissatisfaction with the firm.
    `reason` is a short lowercase tag such as "pain" or "considering another firm"."""

    type: Literal["CLIENT_FLAG"] = "CLIENT_FLAG"
    reason: str


class CheckinOk(Signal):
    """The client replied and nothing is wrong - fine, no change, all good. Schedules
    the next check-in on the normal cadence."""

    type: Literal["CHECKIN_OK"] = "CHECKIN_OK"


class ClientCheckinWorkflow(BaseWorkflow):
    workflow_type: ClassVar[str] = "client_checkin"
    initial_state: ClassVar[str] = "scheduled"
    retry_policy: ClassVar[dict[str, Any]] = {"no_answer": {"schedule": ["1d", "3d"]}, "cadence": "14d"}
    response_deadline: ClassVar[str | None] = "2d"
    resolution_options: ClassVar[list[dict[str, str]]] = [
        {"action": "spoke_to_client", "label": "spoke to client"},
    ]
    domain_signals: ClassVar[list[type[Signal]]] = [ClientFlag, CheckinOk]
    keyword_rules: ClassVar[list[KeywordRule]] = [
        rule(
            "client_flag",
            r"\b(worse|getting worse|pain|hospital|emergency|surgery|depress\w*|anxi\w*|suicid\w*|can'?t sleep|another (lawyer|attorney)|fire you|unhappy|frustrated)\b",
            lambda m, t, c: ClientFlag(reason=m.group(0).lower(), confidence=0.85, evidence=m.group(0)),
        ),
        rule(
            "checkin_ok",
            r"\b(fine|good|well|okay|ok|same|no change|better|all good|nothing new|hanging in)\b",
            lambda m, t, c: CheckinOk(confidence=0.8, evidence=m.group(0)),
        ),
    ]

    async def on_wake(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        c = instance.context
        name = await ctx.contact_name(c["client_contact_id"])
        await ctx.send(
            instance,
            c["client_contact_id"],
            f"Hi {name}, this is a quick check-in from your legal team. How are you feeling, and is there anything new we should know about?",
            channel=c.get("client_channel", "sms"),
        )
        await ctx.transition(instance, "awaiting_reply")

    async def handle_domain_signal(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        c = instance.context
        if isinstance(signal, ClientFlag):
            await ctx.send(instance, c["client_contact_id"], "Thanks for letting us know - a member of your legal team will reach out to you directly.", channel=c.get("client_channel", "sms"))
            await ctx.create_review_task(instance, f"client_flag:{signal.reason}", ["call client", "notify attorney"], extra={"evidence": signal.evidence})
        elif isinstance(signal, CheckinOk):
            instance.attempt_count = 0
            await ctx.transition(instance, "scheduled")
            await ctx.schedule_wake_in(instance, self.retry_policy["cadence"], reason="cadence")
        else:
            await super().handle_domain_signal(instance, signal, ctx)

    async def on_generic_outcome(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        if signal.type == "RESCHEDULE":
            await ctx.transition(instance, "scheduled")

    async def on_review_resolved(self, instance, task, ctx) -> None:
        """A team member handled it personally - drop back onto the normal cadence
        rather than starting the chase again."""
        if (task.resolution or {}).get("action") == "spoke_to_client":
            instance.attempt_count = 0
            await ctx.transition(instance, "scheduled")
            await ctx.schedule_wake_in(instance, self.retry_policy["cadence"], reason="cadence")
        else:
            await super().on_review_resolved(instance, task, ctx)
