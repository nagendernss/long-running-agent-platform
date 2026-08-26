"""The plan's standing test: "does adding a workflow require touching engine code?"

This file defines a THIRD workflow entirely inside the test - new states, a new
domain signal, its own retry policy and its own keyword rule - and runs it through
the unmodified Engine, Scheduler, write-back resolver and review queue.
"""
from __future__ import annotations

import inspect
from datetime import timedelta
from typing import Any, ClassVar, Literal

import pytest_asyncio

import app.engine as engine_module
import app.scheduling.scheduler as scheduler_module
from app.agent_brain import KeywordRule, rule
from app.channels import MockChannel
from app.clock import FakeClock
from app.db.models import WorkflowInstance
from app.runtime import build_runtime
from app.signals import Signal
from app.workflows.base import BaseWorkflow, WorkflowContext
from app.workflows.registry import default_registry
from tests.helpers import inbound, load, review_tasks, tick


class DeadlineConfirmed(Signal):
    type: Literal["DEADLINE_CONFIRMED"] = "DEADLINE_CONFIRMED"
    court: str | None = None


class CourtDeadlineWorkflow(BaseWorkflow):
    """Workflow #3: chase a court clerk to confirm a filing deadline."""

    workflow_type: ClassVar[str] = "court_deadline_confirmation"
    initial_state: ClassVar[str] = "awaiting_confirmation"
    retry_policy: ClassVar[dict[str, Any]] = {"no_answer": {"schedule": ["1d"]}}
    domain_signals: ClassVar[list[type[Signal]]] = [DeadlineConfirmed]
    keyword_rules: ClassVar[list[KeywordRule]] = [
        rule(
            "deadline_confirmed",
            r"\b(deadline (is )?confirmed|docket confirms|hearing (is )?set)\b",
            lambda m, t, c: DeadlineConfirmed(court="clerk", confidence=0.9, evidence=m.group(0)),
        )
    ]

    async def on_wake(self, instance: WorkflowInstance, ctx: WorkflowContext) -> None:
        await ctx.send(instance, instance.context["clerk_contact_id"], "Confirming the filing deadline, please.", channel="email")
        await ctx.transition(instance, "awaiting_confirmation")

    async def handle_domain_signal(self, instance: WorkflowInstance, signal: Signal, ctx: WorkflowContext) -> None:
        if isinstance(signal, DeadlineConfirmed):
            await ctx.send(instance, instance.context["client_contact_id"], "Your filing deadline is confirmed.", channel="sms")
            await ctx.complete(instance, outcome="deadline_confirmed")
        else:
            await super().handle_domain_signal(instance, signal, ctx)


@pytest_asyncio.fixture(loop_scope="session")
async def rt3(settings, schema):
    registry = default_registry()
    registry.register(CourtDeadlineWorkflow())  # <- the ONLY wiring a new workflow needs
    runtime = await build_runtime(settings, clock=FakeClock(), channel=MockChannel(), registry=registry, durable=True)
    try:
        yield runtime
    finally:
        await runtime.close()


async def test_third_workflow_runs_on_the_unmodified_engine(rt3, seed):
    ctx = {
        "target_contact_id": str(seed.provider_id),
        "clerk_contact_id": str(seed.provider_id),
        "client_contact_id": str(seed.client_id),
    }
    async with rt3.session_factory() as s, s.begin():
        inst = await rt3.engine.start_instance(s, "court_deadline_confirmation", case_id=seed.case_id, context=ctx)
        iid = inst.id
    assert rt3.channel.outbox[-1].channel == "email"

    # generic signals: engine handles them for a workflow it has never seen
    await inbound(rt3, iid, "no answer")
    inst = await load(rt3, iid)
    assert inst.attempt_count == 1 and inst.wake_reason == "retry"
    assert inst.next_wake_at - rt3.clock.now() <= timedelta(days=4)

    rt3.clock.set(inst.next_wake_at + timedelta(minutes=1))
    assert [r for _, r in await tick(rt3)] == ["executed"]

    await inbound(rt3, iid, "no answer")  # policy exhausted after one retry
    assert (await load(rt3, iid)).status == "blocked"
    tasks = await review_tasks(rt3, iid)
    assert [t.reason for t in tasks] == ["stalled_no_response"]

    async with rt3.session_factory() as s, s.begin():
        await rt3.engine.resolve_review_task(s, tasks[0].id, {"action": "retry"}, resolved_by="staff")
    await tick(rt3)

    # generic write-back works with no per-workflow configuration
    await inbound(rt3, iid, "wrong number, reach us at 555-0142")
    from app.db.facts import get_entity_field

    async with rt3.session_factory() as s:
        assert await get_entity_field(s, "contact", seed.provider_id, "phone") == "5550142"

    # domain signal routed to the new workflow only
    await inbound(rt3, iid, "the deadline is confirmed for next month")
    inst = await load(rt3, iid)
    assert inst.status == "completed" and inst.state == "completed"
    assert "deadline is confirmed" in rt3.channel.outbox[-1].body


def test_engine_and_scheduler_never_name_a_concrete_workflow():
    """Structural guard: the layer boundary is checkable, not just aspirational."""
    forbidden = ["medical_records", "client_checkin", "RECORDS_RECEIVED", "AUTH_REQUIRED", "CLIENT_FLAG", "hipaa"]
    for module in (engine_module, scheduler_module):
        source = inspect.getsource(module).lower()
        for token in forbidden:
            assert token.lower() not in source, f"{module.__name__} references {token}"
