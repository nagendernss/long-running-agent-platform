"""The outreach template: send to one contact, wait, chase, hand off.

`client_checkin` and `contact_update` were two modules describing this same machine.
The only real difference between them is what a positive reply means - a check-in
loops on a cadence, a notice finishes - so that is a parameter (`on_reply`), not a
module. Everything else they differed in (the ladder, the deadline, what counts as
concerning) was already data pretending to be code.

A *template* is this class. A *workflow type* is a row naming it plus a spec; see
`app/workflows/types.py`. Workflows with real branching - several parties, per-signal
side effects, like medical_records_followup - stay as code.
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from app.agent_brain import KeywordRule, rule
from app.db.models import WorkflowInstance
from app.signals import Signal
from app.workflows.base import BaseWorkflow, WorkflowContext

DEFAULT_ACK_PHRASES = [
    "got it", "received", "thanks", "thank you", "noted", "acknowledged",
    "will do", "understood", "ok", "okay", "fine", "all good", "no change", "nothing new",
]


class Acknowledged(Signal):
    """The contact replied and nothing is wrong - they confirmed, or said all is well."""

    type: Literal["ACKNOWLEDGED"] = "ACKNOWLEDGED"


class Flagged(Signal):
    """The reply contains something a person must see for this particular workflow.
    `reason` is a short lowercase tag naming what was raised."""

    type: Literal["FLAGGED"] = "FLAGGED"
    reason: str


class OutreachSpec(BaseModel):
    """What a person fills in to create a workflow type.

    Retries are a count and an interval rather than a duration list, because that is
    how the work is actually described - "chase three times, a couple of days apart".
    `ladder()` expands it into the schedule the Engine already understands.
    """

    message: str
    description: str | None = None
    channel: Literal["call", "sms", "email"] = "sms"
    recipient_key: str = "target_contact_id"
    # Who this type contacts. A check-in always reaches a client, a records chase
    # always a provider - so it belongs to the type, not to each agent started from it.
    contact_role: Literal["client", "provider", "staff"] = "client"

    retry_count: int = Field(default=2, ge=0, le=10)
    retry_interval_days: int = Field(default=2, ge=1, le=90)
    response_deadline_days: int = Field(default=2, ge=1, le=90)
    # Escape hatch for a ladder the form cannot express, e.g. backing off 2d, 5d, 14d.
    # The builder writes count + interval; this wins when set.
    retry_schedule: list[str] | None = None

    # Instances can carry their own message, channel, reminder and require_ack, so one
    # type can be started with per-agent wording without defining a new type.
    expect_reply: bool = True
    awaiting_state: str = "awaiting_reply"
    idle_state: str = "scheduled"

    on_reply: Literal["complete", "repeat"] = "complete"
    repeat_every_days: int | None = Field(default=None, ge=1, le=365)
    reminder_days: int | None = Field(default=None, ge=1, le=90)

    ack_phrases: list[str] = Field(default_factory=lambda: list(DEFAULT_ACK_PHRASES))
    escalate_keywords: list[str] = Field(default_factory=list)
    escalate_reason: str = "flagged"
    flag_reply: str = "Thanks for letting us know - someone from the team will be in touch shortly."

    @model_validator(mode="after")
    def _repeat_needs_a_cadence(self) -> "OutreachSpec":
        if self.on_reply == "repeat" and not self.repeat_every_days:
            raise ValueError("on_reply='repeat' needs repeat_every_days")
        return self

    # -- derived, so the Engine keeps seeing the shapes it already knows -------------
    def ladder(self) -> list[str]:
        if self.retry_schedule is not None:
            return list(self.retry_schedule)
        return [f"{self.retry_interval_days}d"] * self.retry_count

    @property
    def repeat_every(self) -> str | None:
        return f"{self.repeat_every_days}d" if self.repeat_every_days else None

    @property
    def reminder(self) -> str | None:
        return f"{self.reminder_days}d" if self.reminder_days else None


def _phrase_pattern(phrases: list[str]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(re.escape(p) for p in phrases) + r")\b", re.IGNORECASE)


class OutreachTemplate(BaseWorkflow):
    template_name: ClassVar[str] = "outreach"
    spec_model: ClassVar[type[BaseModel]] = OutreachSpec

    def __init__(self, workflow_type: str, spec: OutreachSpec):
        self.workflow_type = workflow_type
        self.spec = spec
        # A repeating workflow starts idle and moves to awaiting on each send; a
        # one-shot starts by waiting for the acknowledgement.
        self.initial_state = spec.idle_state if spec.on_reply == "repeat" else spec.awaiting_state
        self.contact_role = spec.contact_role
        self.retry_policy = {"no_answer": {"schedule": spec.ladder()}}
        self.response_deadline = f"{spec.response_deadline_days}d"
        self.domain_signals = [Acknowledged] + ([Flagged] if spec.escalate_keywords else [])
        self.keyword_rules = self._build_rules()
        self.resolution_options = [{"action": "acknowledged", "label": "they confirmed"}]
        self.prompt_notes = self._build_prompt_notes()

    # -- generated from the spec ----------------------------------------------------
    def _build_rules(self) -> list[KeywordRule]:
        rules = [
            rule(
                "acknowledged",
                _phrase_pattern(self.spec.ack_phrases).pattern,
                lambda m, t, c: Acknowledged(confidence=0.85, evidence=m.group(0)),
            )
        ]
        if self.spec.escalate_keywords:
            rules.insert(0, rule(  # checked first: a concerning reply outranks a polite one
                "flagged",
                _phrase_pattern(self.spec.escalate_keywords).pattern,
                lambda m, t, c: Flagged(reason=m.group(0).lower(), confidence=0.85, evidence=m.group(0)),
            ))
        return rules

    def _build_prompt_notes(self) -> str | None:
        if not self.spec.escalate_keywords:
            return None
        topics = ", ".join(self.spec.escalate_keywords)
        return (
            f"For this workflow, treat a reply as FLAGGED when it raises any of: {topics}. "
            "Use ACKNOWLEDGED only when the contact replied and nothing needs attention."
        )

    def render(self, template: str, context: dict[str, Any], **extra: Any) -> str:
        """Fill {placeholders} from the instance context. A typo in a builder form must
        not kill a live workflow, so unknown names are left as written."""
        values: dict[str, Any] = {**context, **extra}

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(values[key]) if key in values else match.group(0)

        return re.sub(r"\{(\w+)\}", replace, template)

    # -- lifecycle ------------------------------------------------------------------
    async def on_wake(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        c = instance.context
        contact_id = c[self.spec.recipient_key]
        body = self.render(
            c.get("message") or self.spec.message, c, contact_name=await ctx.contact_name(contact_id)
        )
        await ctx.send(instance, contact_id, body, channel=c.get("channel") or self.spec.channel)

        if not c.get("require_ack", self.spec.expect_reply):
            await ctx.complete(instance, outcome="delivered")   # fire and forget
            return

        await ctx.transition(instance, self.spec.awaiting_state)
        reminder = c.get("reminder") or self.spec.reminder
        if reminder and not c.get("reminded"):
            instance.context = {**instance.context, "reminded": True}
            await ctx.schedule_wake_in(instance, reminder, reason="reminder")

    async def handle_domain_signal(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        if isinstance(signal, Acknowledged):
            await self._on_positive_reply(instance, ctx)
        elif isinstance(signal, Flagged):
            await ctx.send(
                instance, instance.context[self.spec.recipient_key],
                self.render(self.spec.flag_reply, instance.context),
                channel=instance.context.get("channel") or self.spec.channel,
            )
            await ctx.create_review_task(
                instance, f"{self.spec.escalate_reason}:{signal.reason}",
                ["call them", "notify the case owner"], extra={"evidence": signal.evidence},
            )
        else:
            await super().handle_domain_signal(instance, signal, ctx)

    async def _on_positive_reply(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        if self.spec.on_reply == "complete":
            await ctx.complete(instance, outcome="acknowledged")
            return
        instance.attempt_count = 0
        instance.context = {k: v for k, v in instance.context.items() if k != "reminded"}
        await ctx.transition(instance, self.spec.idle_state)
        await ctx.schedule_wake_in(instance, self.spec.repeat_every, reason="cadence")

    async def on_generic_outcome(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        """A repeating workflow that was asked to come back later is idle, not waiting
        on a reply - the state should say so."""
        if signal.type == "RESCHEDULE" and self.spec.on_reply == "repeat":
            await ctx.transition(instance, self.spec.idle_state)

    async def on_review_resolved(self, instance: WorkflowInstance, task, ctx: WorkflowContext) -> None:
        if (task.resolution or {}).get("action") == "acknowledged":
            await self._on_positive_reply(instance, ctx)
        else:
            await super().on_review_resolved(instance, task, ctx)


TEMPLATES: dict[str, type[OutreachTemplate]] = {OutreachTemplate.template_name: OutreachTemplate}
