"""End-to-end scenario for the medical records follow-up workflow (plan Phase 9, steps 2-7)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.facts import get_current_value
from app.db.models import Contact, EntityFactVersion
from app.scheduling.constraints import apply_scheduling_constraints
from tests.helpers import events, inbound, load, medical_context, review_tasks, tick


async def start(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed))
        return inst.id


async def expected_wake(rt, seed, delta):
    async with rt.session_factory() as s:
        provider = await s.get(Contact, seed.provider_id)
    return apply_scheduling_constraints(rt.clock.now() + delta, provider)


async def test_initial_send_goes_to_provider_and_nothing_is_due(rt, seed):
    iid = await start(rt, seed)
    outbox = rt.channel.outbox
    assert len(outbox) == 1 and outbox[0].address == seed.provider_phone and outbox[0].channel == "call"
    inst = await load(rt, iid)
    assert inst.state == "awaiting_reply" and inst.status == "active"
    # sending arms a response deadline: silence is an outcome, not a dead end
    assert inst.wake_reason == "response_timeout"
    assert timedelta(days=3) <= inst.next_wake_at - rt.clock.now() <= timedelta(days=6)
    assert await tick(rt) == []
    assert (await events(rt, iid))[:3] == ["instance_created", "attempt_started", "message_sent"]


async def test_wrong_number_writes_versioned_fact_and_corrects_future_sends(rt, seed):
    iid = await start(rt, seed)
    sigs = await inbound(rt, iid, "wrong number, it's actually 555-0199")
    assert [s.type for s in sigs] == ["ENTITY_UPDATE"]

    async with rt.session_factory() as s:
        facts = list((await s.execute(select(EntityFactVersion))).scalars().all())
        assert len(facts) == 1
        f = facts[0]
        assert (f.entity_type, f.entity_id, f.field, f.old_value, f.new_value, f.status) == (
            "contact", seed.provider_id, "phone", seed.provider_phone, "5550199", "applied"
        )
        assert f.source_event_id is not None
        assert await get_current_value(s, "contact", seed.provider_id, "phone") == "5550199"
        provider = await s.get(Contact, seed.provider_id)
        assert provider.phone == seed.provider_phone  # base column untouched; fact store wins

    # next outreach uses the corrected number without any workflow code knowing about it
    await inbound(rt, iid, "call back in 2 days")
    rt.clock.advance(timedelta(days=3))
    assert [r for _, r in await tick(rt)] == ["executed"]
    assert rt.channel.outbox[-1].address == "5550199"


async def test_reschedule_sets_wake_14d_out_resets_attempts_and_updates_client(rt, seed):
    iid = await start(rt, seed)
    await inbound(rt, iid, "no answer")  # bump attempt_count first so we can see the reset
    assert (await load(rt, iid)).attempt_count == 1

    sigs = await inbound(rt, iid, "records not available for 2 weeks")
    assert [s.type for s in sigs] == ["RESCHEDULE"]
    inst = await load(rt, iid)
    assert inst.attempt_count == 0
    assert inst.wake_reason == "dynamic_reschedule"
    assert inst.next_wake_at == await expected_wake(rt, seed, timedelta(days=14))
    assert timedelta(days=14) <= inst.next_wake_at - rt.clock.now() <= timedelta(days=17)
    client_msgs = [m for m in rt.channel.outbox if m.address == seed.client_phone]
    assert "asked us to follow up" in client_msgs[-1].body

    # poller: not due yet, then due after the virtual clock passes next_wake_at
    assert await tick(rt) == []
    rt.clock.advance(timedelta(days=15))
    assert [r for _, r in await tick(rt)] == ["executed"]
    inst = await load(rt, iid)
    assert rt.channel.outbox[-1].address == seed.provider_phone
    assert inst.wake_reason == "response_timeout"      # the follow-up now has its own deadline
    assert await tick(rt) == []                        # nothing else is due yet


async def test_no_answer_follows_retry_schedule_then_escalates(rt, seed):
    iid = await start(rt, seed)
    for attempt, days in enumerate(["2d", "5d", "14d"], start=1):
        delta = timedelta(days=int(days[:-1]))
        expected = await expected_wake(rt, seed, delta)
        await inbound(rt, iid, "no answer")
        inst = await load(rt, iid)
        assert inst.attempt_count == attempt
        assert inst.wake_reason == "retry"
        assert inst.next_wake_at == expected
        assert "no answer" in rt.channel.outbox[-1].body and rt.channel.outbox[-1].address == seed.client_phone
        rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
        assert [r for _, r in await tick(rt)] == ["executed"]
        assert rt.channel.outbox[-1].address == seed.provider_phone

    # 4th no-answer: schedule exhausted -> review task, instance blocked, client told
    await inbound(rt, iid, "voicemail again, no answer")
    inst = await load(rt, iid)
    assert inst.status == "blocked" and inst.next_wake_at is None
    tasks = await review_tasks(rt, iid)
    assert len(tasks) == 1 and tasks[0].reason == "stalled_no_response" and tasks[0].status == "pending"
    assert "taking over" in rt.channel.outbox[-1].body
    assert "escalated" in await events(rt, iid)

    # staff resolves with "retry" -> instance active again, outreach resumes now
    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(s, tasks[0].id, {"action": "retry"}, resolved_by="paralegal@firm")
    inst = await load(rt, iid)
    assert inst.status == "active" and inst.attempt_count == 0 and inst.wake_reason == "resume_after_review"
    assert [r for _, r in await tick(rt)] == ["executed"]


async def test_records_received_completes_and_notifies_client(rt, seed):
    iid = await start(rt, seed)
    await inbound(rt, iid, "Here are the records, attached as PDF", channel="email")
    inst = await load(rt, iid)
    assert inst.status == "completed" and inst.state == "completed"
    assert "sent your medical records" in rt.channel.outbox[-1].body
    # further inbound is ignored
    assert await inbound(rt, iid, "no answer") == []


async def test_auth_required_needs_client_action_human_in_loop(rt, seed):
    iid = await start(rt, seed)
    await inbound(rt, iid, "We need a signed HIPAA authorization from the patient before we can release anything")
    inst = await load(rt, iid)
    assert inst.state == "awaiting_client_auth" and inst.status == "blocked"
    assert inst.context["auth_required"] is True
    assert "HIPAA authorization" in rt.channel.outbox[-1].body and rt.channel.outbox[-1].address == seed.client_phone
    tasks = await review_tasks(rt, iid)
    assert [t.reason for t in tasks] == ["auth_required"]

    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(s, tasks[0].id, {"action": "auth_obtained"}, resolved_by="paralegal@firm")
    inst = await load(rt, iid)
    assert inst.status == "active" and inst.context["auth_obtained"] is True
    assert [r for _, r in await tick(rt)] == ["executed"]
    last = rt.channel.outbox[-1]
    assert last.address == seed.provider_phone and "HIPAA authorization is attached" in last.body
    assert (await load(rt, iid)).state == "awaiting_reply"


async def test_unrecognized_reply_escalates_to_human(rt, seed):
    iid = await start(rt, seed)
    sigs = await inbound(rt, iid, "asdf qwerty ???")
    assert [s.type for s in sigs] == ["NEEDS_HUMAN"]
    assert (await load(rt, iid)).status == "blocked"
    assert [t.reason for t in await review_tasks(rt, iid)] == ["unrecognized_reply"]


async def test_authorization_signed_is_only_offered_when_one_was_asked_for(rt, seed, client=None):
    """Reported from the dashboard: a $45 payment task carried a button reading
    "authorization signed" on a chase where nobody had ever mentioned an authorization.
    Every review row rendered the workflow's whole list of endings, whatever the row
    was about. A button that does not apply gets pressed, and then the file says
    something that did not happen."""
    from app.workflows.medical_records import MedicalRecordsFollowupWorkflow

    workflow = MedicalRecordsFollowupWorkflow()
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        offered = {o["action"] for o in workflow.outcomes_for(inst)}
        assert offered == {"records_received"}, "nothing is waiting on a form"

        inst.context = {**inst.context, "auth_required": True}
        offered = {o["action"] for o in workflow.outcomes_for(inst)}
        assert offered == {"records_received", "auth_obtained"}


async def test_recording_an_outcome_that_does_not_apply_is_refused(rt, seed):
    """The guard is on the engine too, not only the template - the button is gone, and
    a hand-rolled POST still cannot record a signature nobody asked for."""
    import pytest

    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        iid = inst.id

    async with rt.session_factory() as s, s.begin():
        with pytest.raises(ValueError, match="auth_obtained"):
            await rt.engine.record_outcome(s, iid, "auth_obtained", resolved_by="dashboard")
