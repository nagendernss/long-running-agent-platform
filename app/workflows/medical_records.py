"""Reference workflow #1: medical records follow-up with a provider, keeping the
client updated after every outreach outcome, escalating to staff when the client
has to do something (e.g. sign a HIPAA authorization).

Instance context contract:
    target_contact_id   - provider (who we chase)          [used by Engine for scheduling constraints]
    provider_contact_id - same as target
    client_contact_id   - client to keep informed
    provider_channel    - call | sms | email   (default call)
    client_channel      - call | sms | email   (default sms)
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from app.agent_brain import KeywordRule, rule
from app.db.models import ReviewTask, WorkflowInstance
from app.signals import NoAnswer, Reschedule, Signal
from app.workflows.base import BaseWorkflow, WorkflowContext


# --- domain signals (only this workflow ever sees these) ---------------------------
class RecordsReceived(Signal):
    type: Literal["RECORDS_RECEIVED"] = "RECORDS_RECEIVED"


class AuthRequired(Signal):
    type: Literal["AUTH_REQUIRED"] = "AUTH_REQUIRED"


class RequestDenied(Signal):
    type: Literal["REQUEST_DENIED"] = "REQUEST_DENIED"
    reason: Optional[str] = None


def _fmt(dt) -> str:
    return dt.strftime("%b %d") if dt else "soon"


class MedicalRecordsFollowupWorkflow(BaseWorkflow):
    workflow_type: ClassVar[str] = "medical_records_followup"
    initial_state: ClassVar[str] = "awaiting_reply"
    retry_policy: ClassVar[dict[str, Any]] = {"no_answer": {"schedule": ["2d", "5d", "14d"]}}
    domain_signals: ClassVar[list[type[Signal]]] = [RecordsReceived, AuthRequired, RequestDenied]
    keyword_rules: ClassVar[list[KeywordRule]] = [
        rule(
            "records_received",
            r"\b(records (are |have been |were )?(attached|sent|enclosed|mailed|faxed|uploaded|on (the|their) way)|here are the records|sent (over )?the records|sending the records)\b",
            lambda m, t, c: RecordsReceived(confidence=0.9, evidence=m.group(0)),
        ),
        rule(
            "auth_required",
            r"\b(authorization|hipaa|signed release|release form|consent form|patient consent)\b",
            lambda m, t, c: AuthRequired(confidence=0.85, evidence=m.group(0)),
        ),
        rule(
            "request_denied",
            r"\b(denied|refuse|cannot release|can'?t release|will not release|won'?t release|not able to release|no such patient)\b",
            lambda m, t, c: RequestDenied(reason=m.group(0), confidence=0.85, evidence=m.group(0)),
        ),
    ]

    # -- outreach ------------------------------------------------------------------
    async def on_wake(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        c = instance.context
        provider, client = await ctx.contact_name(c["provider_contact_id"]), await ctx.contact_name(c["client_contact_id"])
        body = f"Hi {provider}, following up on the medical records request for {client}. Could you send them over at your earliest convenience?"
        if c.get("auth_obtained"):
            body += " The signed HIPAA authorization is attached."
        await ctx.send(instance, c["provider_contact_id"], body, channel=c.get("provider_channel", "call"))
        await ctx.transition(instance, "awaiting_reply")

    # -- domain signals ------------------------------------------------------------
    async def handle_domain_signal(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        c = instance.context
        provider = await ctx.contact_name(c["provider_contact_id"])
        client_channel = c.get("client_channel", "sms")

        if isinstance(signal, RecordsReceived):
            await ctx.send(instance, c["client_contact_id"], f"Good news - {provider} has sent your medical records. Our team is reviewing them now.", channel=client_channel)
            await ctx.complete(instance, outcome="records_received")

        elif isinstance(signal, AuthRequired):
            instance.context = {**c, "auth_required": True}
            await ctx.transition(instance, "awaiting_client_auth")
            await ctx.send(instance, c["client_contact_id"], f"{provider} needs a signed HIPAA authorization before releasing your records. Someone from our team will send you the form to sign.", channel=client_channel)
            await ctx.create_review_task(instance, "auth_required", ["send HIPAA form to client", "call client"])

        elif isinstance(signal, RequestDenied):
            await ctx.send(instance, c["client_contact_id"], f"{provider} declined our records request. An attorney is reviewing next steps and will be in touch.", channel=client_channel)
            await ctx.create_review_task(instance, "request_denied", ["attorney follow-up", "subpoena"], extra={"reason": signal.reason})

        else:
            await super().handle_domain_signal(instance, signal, ctx)

    # -- client updates after engine-handled outcomes -------------------------------
    async def on_generic_outcome(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        c = instance.context
        client_id = c.get("client_contact_id")
        if not client_id:
            return
        provider = await ctx.contact_name(c["provider_contact_id"])
        channel = c.get("client_channel", "sms")
        if isinstance(signal, NoAnswer):
            if instance.status == "blocked":
                body = f"We've tried {instance.attempt_count + 1} times to reach {provider} for your records without success. A team member is taking over personally."
            else:
                body = f"Update: we called {provider} for your records - no answer. We'll try again on {_fmt(instance.next_wake_at)}."
            await ctx.send(instance, client_id, body, channel=channel)
        elif isinstance(signal, Reschedule):
            await ctx.send(instance, client_id, f"Update: {provider} asked us to follow up on {_fmt(instance.next_wake_at)}. We'll keep you posted.", channel=channel)

    # -- staff resolutions ---------------------------------------------------------
    async def on_review_resolved(self, instance: WorkflowInstance, task: ReviewTask, ctx: WorkflowContext) -> None:
        action = (task.resolution or {}).get("action")
        if task.reason == "auth_required" and action == "auth_obtained":
            instance.context = {**instance.context, "auth_required": False, "auth_obtained": True}
            instance.attempt_count = 0
            await ctx.schedule_wake(instance, ctx.now, reason="resume_after_auth")
        else:
            await super().on_review_resolved(instance, task, ctx)
