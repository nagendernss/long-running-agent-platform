"""Third-party prerequisites: "pay this fee first", "submit through our portal",
"don't phone us, email the request instead".

All three are generic - the Engine handles them for any workflow, and a human
closing the loop resumes the instance carrying the reference forward.
"""
from __future__ import annotations

from datetime import timedelta

from app.db.facts import get_entity_field
from app.signals import ActionRequired, EntityUpdate
from tests.helpers import events, load, medical_context, review_tasks, tick


async def start(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        return inst.id


async def advance(rt, iid, signals):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.advance_instance(s, inst, signals)


async def test_records_fee_blocks_then_a_human_pays_and_outreach_resumes(rt, seed):
    iid = await start(rt, seed)
    rt.channel.outbox.clear()

    await advance(rt, iid, [ActionRequired(
        action_type="payment",
        summary="$45 records fee before release",
        details={"amount": "$45", "payee": "Mercy Health Information Management", "url": "pay.mercy.example"},
        confidence=0.95, evidence="There is a $45 records fee",
    )])

    inst = await load(rt, iid)
    # parked on the instance, so it survives a restart and is visible to staff
    req = inst.context["pending_requirement"]
    assert req["action_type"] == "payment" and req["details"]["amount"] == "$45"
    assert inst.status == "blocked" and inst.next_wake_at is None, "must stop chasing while we owe them something"

    tasks = await review_tasks(rt, iid)
    assert [t.reason for t in tasks] == ["action_required:payment"]
    assert tasks[0].context_snapshot["requirement"]["details"]["payee"] == "Mercy Health Information Management"

    # the client is told a fee is holding things up
    client_msgs = [m for m in rt.channel.outbox if m.address == seed.client_phone]
    assert client_msgs and "$45" in client_msgs[-1].body and "records fee" in client_msgs[-1].body

    # a paralegal records the payment with a reference
    rt.channel.outbox.clear()
    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(
            s, tasks[0].id, {"action": "paid", "reference": "chk-1041"}, resolved_by="paralegal@firm"
        )

    inst = await load(rt, iid)
    assert inst.status == "active" and inst.attempt_count == 0
    assert "pending_requirement" not in inst.context
    done = inst.context["completed_requirements"][-1]
    assert done["resolution"]["reference"] == "chk-1041" and done["completed_by"] == "paralegal@firm"
    assert inst.wake_reason == "resume_after_requirement"

    # ...and the next outreach cites it, so the provider cannot ask twice
    assert [r for _, r in await tick(rt)] == ["executed"]
    sent = rt.channel.outbox[-1]
    assert sent.address == seed.provider_phone and "chk-1041" in sent.body
    assert "requirement_completed" in await events(rt, iid)


async def test_portal_submission_uses_the_same_generic_path(rt, seed):
    iid = await start(rt, seed)
    await advance(rt, iid, [ActionRequired(
        action_type="portal_submission",
        summary="Submit the request through the Ciox vendor portal",
        details={"url": "ciox.example/requests"},
        suggested_options=["submit via Ciox", "call vendor"],
        confidence=0.9, evidence="You need to submit this through our vendor portal",
    )])
    tasks = await review_tasks(rt, iid)
    assert [t.reason for t in tasks] == ["action_required:portal_submission"]
    assert tasks[0].suggested_options == ["submit via Ciox", "call vendor"]

    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(s, tasks[0].id, {"action": "submitted"}, resolved_by="staff")
    inst = await load(rt, iid)
    assert inst.status == "active" and inst.context["completed_requirements"][-1]["action_type"] == "portal_submission"


async def test_non_blocking_requirement_records_without_stopping_outreach(rt, seed):
    iid = await start(rt, seed)
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.scheduler.schedule_wake(s, inst, rt.clock.now() + timedelta(days=2), reason="retry")
    await advance(rt, iid, [ActionRequired(
        action_type="document", summary="send a copy of the client's ID when convenient",
        blocks_progress=False, confidence=0.8, evidence="send ID when you can",
    )])
    inst = await load(rt, iid)
    assert inst.status == "active" and inst.next_wake_at is not None, "advisory requirement must not stall the workflow"
    assert inst.context["pending_requirement"]["action_type"] == "document"
    assert not await review_tasks(rt, iid)


async def test_preferred_channel_fact_reroutes_every_later_send(rt, seed):
    """'Do not phone us, email the request instead' - one generic fact, no workflow change."""
    iid = await start(rt, seed)
    assert rt.channel.outbox[-1].channel == "call"  # workflow asked for a call

    await advance(rt, iid, [
        EntityUpdate(entity_type="contact", entity_id=str(seed.provider_id), field="preferred_channel",
                     new_value="email", confidence=0.95, evidence="we don't take these over the phone"),
        EntityUpdate(entity_type="contact", entity_id=str(seed.provider_id), field="email",
                     new_value="roi@mercy.example", confidence=0.95, evidence="email the request to roi@mercy.example"),
    ])
    async with rt.session_factory() as s:
        assert await get_entity_field(s, "contact", seed.provider_id, "preferred_channel") == "email"

    rt.channel.outbox.clear()
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.scheduler.schedule_wake(s, inst, rt.clock.now(), reason="retry")
    assert [r for _, r in await tick(rt)] == ["executed"]

    sent = rt.channel.outbox[-1]
    assert sent.channel == "email" and sent.address == "roi@mercy.example", "workflow said 'call', the fact said 'email'"
    assert "channel_overridden" in await events(rt, iid)


async def test_preferred_channel_is_ignored_when_that_address_is_missing(rt, seed):
    """A contact who asks for email but has no email on file must still be reachable."""
    import uuid as _uuid

    from app.db.models import Contact

    async with rt.session_factory() as s, s.begin():
        phone_only = Contact(
            id=_uuid.uuid4(), name="Fax Only Clinic", role="provider", phone="+15550777",
            email=None, created_at=rt.clock.now(),
        )
        s.add(phone_only)
        await s.flush()
        cid = phone_only.id

    iid = await start(rt, seed)
    await advance(rt, iid, [EntityUpdate(
        entity_type="contact", entity_id=str(cid), field="preferred_channel",
        new_value="email", confidence=0.95, evidence="email us",
    )])

    rt.channel.outbox.clear()
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.ctx(s).send(inst, cid, "hello", channel="sms")

    sent = rt.channel.outbox[-1]
    assert sent.channel == "sms" and sent.address == "+15550777"
    assert "channel_overridden" not in await events(rt, iid)


async def test_requirement_completion_is_ignored_without_a_pending_requirement(rt, seed):
    """A 'paid' resolution on an unrelated task must not resurrect a closed instance."""
    iid = await start(rt, seed)
    for _ in range(4):
        async with rt.session_factory() as s, s.begin():
            await rt.engine.handle_inbound(s, iid, "no answer")
    tasks = await review_tasks(rt, iid)
    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(s, tasks[-1].id, {"action": "paid"}, resolved_by="staff")
    inst = await load(rt, iid)
    assert "completed_requirements" not in inst.context
    assert inst.wake_reason != "resume_after_requirement"


# ---------------------------------------------------------------- fallback coverage
async def test_rule_fallback_also_recognises_fees_portals_and_channel_switches():
    """The fallback is what runs during a provider outage or a 429, so the common
    third-party prerequisites must not depend on the LLM being reachable."""
    from app.agent_brain import RuleBasedAgentBrain
    from app.workflows.registry import default_registry

    registry = default_registry()
    brain = RuleBasedAgentBrain(domain_rules_for=lambda wt: registry.get(wt).keyword_rules)
    ctx = {"workflow_type": "medical_records_followup", "target_contact_id": "11111111-1111-1111-1111-111111111111"}

    fee = await brain.extract_signals("There is a $45 records fee, pay at pay.mercy.example", ctx)
    req = next(s for s in fee if s.type == "ACTION_REQUIRED")
    assert req.action_type == "payment" and req.details["amount"] == "$45"
    assert req.details["url"] == "pay.mercy.example"

    portal = await brain.extract_signals("Submit this through our vendor portal, Ciox, at ciox.example/requests", ctx)
    assert next(s for s in portal if s.type == "ACTION_REQUIRED").action_type == "portal_submission"

    # one message, two facts: how to reach them AND where
    switch = await brain.extract_signals(
        "We do not take these over the phone. Email the request to roi@mercyhealth.example.", ctx
    )
    updates = {s.field: s.new_value for s in switch if s.type == "ENTITY_UPDATE"}
    assert updates["preferred_channel"] == "email"
    assert updates["email"] == "roi@mercyhealth.example", "trailing sentence punctuation must be stripped"


async def test_wake_is_rearmed_when_the_last_review_task_clears(rt, seed):
    """Two open tasks: the instance stays blocked after the first is resolved, and the
    durable job is re-issued once the second clears - recovery must not rely on the
    poller alone."""
    iid = await start(rt, seed)
    await advance(rt, iid, [ActionRequired(
        action_type="payment", summary="$45 fee", details={"amount": "$45"},
        confidence=0.95, evidence="fee",
    )])
    await advance(rt, iid, [__import__("app.signals", fromlist=["NeedsHuman"]).NeedsHuman(
        reason="caller_requested_human", confidence=0.9, evidence="let me speak to someone",
    )])
    tasks = await review_tasks(rt, iid)
    assert len(tasks) == 2

    fee = next(t for t in tasks if t.reason.startswith("action_required:"))
    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(s, fee.id, {"action": "paid", "reference": "chk-9"}, resolved_by="staff")
    inst = await load(rt, iid)
    assert inst.status == "blocked", "still blocked: another human task is open"
    assert await tick(rt) == []

    other = next(t for t in tasks if not t.reason.startswith("action_required:"))
    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(s, other.id, {"action": "retry"}, resolved_by="staff")
    inst = await load(rt, iid)
    assert inst.status == "active" and inst.next_wake_at is not None
    assert inst.wake_job_id is not None, "a fresh durable job must be issued on unblock"
    assert [r for _, r in await tick(rt)] == ["executed"]
    assert "chk-9" in rt.channel.outbox[-1].body
