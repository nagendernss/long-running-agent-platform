"""End-to-end demo on a virtual clock. No UI needed to prove the design works.

    python scripts/demo.py                    # embedded postgres (pgserver)
    python scripts/demo.py --db postgresql://user:pass@localhost:5432/hellocounsel

Story: a law firm chases a provider for a client's medical records, keeps the client
updated after every outcome, absorbs a wrong-number correction and a "call us in two
weeks", exhausts its retry ladder into a human review task, and then runs the
recurring client check-in workflow through the same engine.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402

from app.channels import MockChannel  # noqa: E402
from app.clock import FakeClock  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.facts import get_current_value, get_entity_field  # noqa: E402
from app.db.models import CaseRecord, Contact, EntityFactVersion, ReviewTask, WorkflowInstance  # noqa: E402
from app.events import list_events  # noqa: E402
from app.runtime import build_runtime, configure_event_loop, ensure_schema  # noqa: E402

BOLD, DIM, GREEN, YELLOW, CYAN, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"


def step(n: str, title: str) -> None:
    print(f"\n{BOLD}{CYAN}== {n}  {title} =={RESET}")


def say(msg: str) -> None:
    print(f"   {msg}")


def ok(msg: str) -> None:
    print(f"   {GREEN}OK{RESET} {msg}")


def clockline(rt) -> None:
    print(f"   {DIM}[virtual clock] {rt.clock.now():%Y-%m-%d %H:%M UTC}{RESET}")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    ok(msg)


async def instance(rt, iid) -> WorkflowInstance:
    async with rt.session_factory() as s:
        return await s.get(WorkflowInstance, iid)


async def inbound(rt, iid, text_, channel="sms", who="provider") -> list:
    print(f"   {YELLOW}<- inbound ({channel}, {who}){RESET} {text_!r}")
    async with rt.session_factory() as s, s.begin():
        sigs = await rt.engine.handle_inbound(s, iid, text_, channel=channel)
    print(f"   {DIM}   brain -> {[x.describe() for x in sigs]}{RESET}")
    return sigs


async def tick(rt) -> list:
    async with rt.session_factory() as s, s.begin():
        results = await rt.engine.run_due(s)
    say(f"{DIM}poller tick -> {[r for _, r in results] or 'nothing due'}{RESET}")
    return results


def drain(rt, label: str = "") -> list:
    msgs, rt.channel.outbox[:] = list(rt.channel.outbox), []
    for m in msgs:
        print(f"   {GREEN}-> outbound ({m.channel} to {m.address}){RESET} {m.body}")
    return msgs


async def pending_reviews(rt, iid=None) -> list[ReviewTask]:
    async with rt.session_factory() as s:
        stmt = select(ReviewTask).where(ReviewTask.status == "pending")
        if iid:
            stmt = stmt.where(ReviewTask.instance_id == iid)
        return list((await s.execute(stmt.order_by(ReviewTask.created_at))).scalars().all())


async def timeline(rt, iid) -> None:
    async with rt.session_factory() as s:
        evs = await list_events(s, iid)
    print(f"\n   {BOLD}Audit trail ({len(evs)} events){RESET}")
    for e in evs:
        detail = {k: v for k, v in e.payload.items() if k in {"outcome", "reason", "to", "at", "field", "new_value", "from", "wait", "attempt", "retry_in", "retry", "status", "resolution"}}
        print(f"   {DIM}{e.created_at:%m-%d %H:%M}{RESET} {e.type:<20} {DIM}{detail or ''}{RESET}")


async def main(db_url: str | None) -> None:
    if db_url is None:
        import pgserver

        srv = pgserver.get_server(Path(ROOT) / ".pgdata")
        srv.psql("DROP DATABASE IF EXISTS hc_demo")
        srv.psql("CREATE DATABASE hc_demo")
        db_url = srv.get_uri("hc_demo")
        say(f"{DIM}embedded postgres: {db_url}{RESET}")

    os.environ.setdefault("FIELD_REGISTRY_PATH", str(ROOT / "config" / "field_registry.yaml"))
    settings = get_settings(db_url)  # picks up AGENT_BRAIN / GEMINI_API_KEY from .env
    await ensure_schema(settings)
    rt = await build_runtime(settings, clock=FakeClock(), channel=MockChannel(), durable=True)
    say(f"{DIM}agent brain: {settings.agent_brain}"
        + (f" ({settings.gemini_model})" if settings.agent_brain == "gemini" else " (keyword baseline)")
        + RESET)
    async with rt.db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE review_task, entity_fact_version, event, workflow_instance, case_record, contact, procrastinate_jobs CASCADE"))

    try:
        # ---------------------------------------------------------------- 1. seed
        step("1", "Seed a case, a client and a provider")
        now = rt.clock.now()
        async with rt.session_factory() as s, s.begin():
            client = Contact(id=uuid.uuid4(), name="Jane Client", role="client", phone="+15550001", email="jane@example.com", created_at=now)
            provider = Contact(id=uuid.uuid4(), name="Mercy Hospital Records", role="provider", phone="+15550100", email="records@mercy.example", timezone="America/Chicago", business_hours={"start": "08:00", "end": "16:00"}, created_at=now)
            s.add_all([client, provider])
            await s.flush()
            case = CaseRecord(id=uuid.uuid4(), client_contact_id=client.id, matter_type="personal_injury", created_at=now)
            s.add(case)
            await s.flush()
            client_id, provider_id, case_id = client.id, provider.id, case.id
        clockline(rt)
        say(f"client={client_id}  provider={provider_id}  case={case_id}")

        # ------------------------------------------------- 2. start the workflow
        step("2", "Start medical_records_followup -> initial outreach to the provider")
        ctx = {
            "target_contact_id": str(provider_id), "provider_contact_id": str(provider_id),
            "client_contact_id": str(client_id), "provider_channel": "call", "client_channel": "sms",
        }
        async with rt.session_factory() as s, s.begin():
            inst = await rt.engine.start_instance(s, "medical_records_followup", case_id=case_id, context=ctx)
            iid = inst.id
        sent = drain(rt)
        check(len(sent) == 1 and sent[0].address == "+15550100", "initial call placed to provider's number on file")
        check((await instance(rt, iid)).state == "awaiting_reply", "state = awaiting_reply")

        # ------------------------------------------------------- 3. poller: idle
        step("3", "Poller tick with nothing due")
        check(await tick(rt) == [], "no instance woke up (nothing scheduled)")

        # ------------------------------------------- 4. wrong number -> fact write-back
        step("4", "Inbound: wrong number, here is the real one -> generic ENTITY_UPDATE")
        await inbound(rt, iid, "wrong number, it's actually 555-0199", channel="call")
        async with rt.session_factory() as s:
            facts = list((await s.execute(select(EntityFactVersion))).scalars().all())
            current = await get_current_value(s, "contact", provider_id, "phone")
            base_row = await s.get(Contact, provider_id)
        check(len(facts) == 1 and facts[0].status == "applied", f"entity_fact_version row written: {facts[0].old_value} -> {facts[0].new_value} (confidence {facts[0].confidence})")
        check(current == "5550199", "get_current_value returns the corrected number")
        check(base_row.phone == "+15550100", "base contact column untouched - facts are versioned, not destructive")

        # ------------------------------- 5. provider asks for a delay -> RESCHEDULE
        step("5", "Inbound: 'records not available for 2 weeks' -> generic RESCHEDULE")
        await inbound(rt, iid, "records not available for 2 weeks, call us back then", channel="call")
        inst = await instance(rt, iid)
        check(inst.attempt_count == 0, "attempt_count reset to 0 (a reply is not a failed attempt)")
        check(timedelta(days=14) <= inst.next_wake_at - rt.clock.now() <= timedelta(days=17), f"next_wake_at ~14d out, clamped into provider business hours: {inst.next_wake_at:%Y-%m-%d %H:%M UTC}")
        drain(rt)  # client got an update about the delay
        check(await tick(rt) == [], "poller: still nothing due")

        # ----------------------------------- 6. time travel -> wake, correct number
        step("6", "Advance the clock past next_wake_at -> instance wakes, uses the corrected number")
        rt.clock.advance(timedelta(days=15))
        clockline(rt)
        results = await tick(rt)
        sent = drain(rt)
        check([r for _, r in results] == ["executed"], "instance woke up")
        check(sent[-1].address == "5550199", "follow-up went to the CORRECTED number - no workflow code knew about the change")

        # ------------------------------------------- 7. retry ladder -> escalation
        step("7", "Three no-answers follow the 2d/5d/14d retry ladder, the 4th escalates")
        for expected in ["2d", "5d", "14d"]:
            await inbound(rt, iid, "no answer, voicemail", channel="call")
            inst = await instance(rt, iid)
            gap = inst.next_wake_at - rt.clock.now()
            check(timedelta(days=int(expected[:-1])) <= gap <= timedelta(days=int(expected[:-1]) + 3), f"attempt {inst.attempt_count}: next retry ~{expected} out ({inst.next_wake_at:%Y-%m-%d %H:%M})")
            drain(rt)  # client kept informed each time
            rt.clock.set(inst.next_wake_at + timedelta(minutes=1))
            await tick(rt)
            drain(rt)
        await inbound(rt, iid, "no answer again", channel="call")
        inst = await instance(rt, iid)
        tasks = await pending_reviews(rt, iid)
        drain(rt)
        check(inst.status == "blocked" and inst.next_wake_at is None, "retry schedule exhausted -> instance blocked, no further wakes")
        check(len(tasks) == 1 and tasks[0].reason == "stalled_no_response", f"review_task created: {tasks[0].reason} (options: {tasks[0].suggested_options})")

        # ---------------------------------- 8. human in the loop resolves the task
        step("8", "Human-in-the-loop: staff resolves the review task -> outreach resumes")
        async with rt.session_factory() as s, s.begin():
            await rt.engine.resolve_review_task(s, tasks[0].id, {"action": "retry", "note": "spoke to records dept, try again"}, resolved_by="paralegal@firm")
        inst = await instance(rt, iid)
        check(inst.status == "active" and inst.attempt_count == 0, "instance unblocked and attempt counter reset by the human decision")
        await tick(rt)
        drain(rt)

        # ------------------------------------ 9. provider needs client action (HITL)
        step("9", "Provider needs a signed HIPAA authorization -> client is told, staff gets a task")
        await inbound(rt, iid, "We need a signed HIPAA authorization from the patient before releasing anything", channel="email")
        inst = await instance(rt, iid)
        drain(rt)
        tasks = await pending_reviews(rt, iid)
        check(inst.state == "awaiting_client_auth" and inst.status == "blocked", "state = awaiting_client_auth, instance blocked on the client")
        check(any(t.reason == "auth_required" for t in tasks), "review_task 'auth_required' raised for staff")
        async with rt.session_factory() as s, s.begin():
            await rt.engine.resolve_review_task(s, [t for t in tasks if t.reason == "auth_required"][0].id, {"action": "auth_obtained"}, resolved_by="paralegal@firm")
        await tick(rt)
        sent = drain(rt)
        check(any("HIPAA authorization is attached" in m.body for m in sent), "after the client signs, the provider is re-contacted WITH the authorization")

        # --------------------------- 9b. the provider wants something from US first
        step("9b", "Provider demands a records fee -> we owe them -> a human pays -> outreach resumes")
        await inbound(rt, iid, "There is a $45 records fee. Pay online at pay.mercy.example before we release anything.", channel="email")
        inst = await instance(rt, iid)
        sent = drain(rt)
        req = inst.context.get("pending_requirement") or {}
        check(req.get("action_type") == "payment", f"requirement parked on the instance: {req.get('summary')} {req.get('details')}")
        check(inst.status == "blocked" and inst.next_wake_at is None, "stopped chasing while the ball is in our court")
        check(any("$45" in m.body and m.address == "+15550001" for m in sent), "client told what is holding their records up")
        tasks = await pending_reviews(rt, iid)
        fee_task = [t for t in tasks if t.reason.startswith("action_required:")][0]
        check(True, f"review_task '{fee_task.reason}' with details {fee_task.context_snapshot['requirement']['details']}")

        step("9c", "Paralegal records the payment with a reference -> agent resumes and cites it")
        async with rt.session_factory() as s, s.begin():
            await rt.engine.resolve_review_task(s, fee_task.id, {"action": "paid", "reference": "chk-1041"}, resolved_by="paralegal@firm")
        inst = await instance(rt, iid)
        check(inst.status == "active" and "pending_requirement" not in inst.context, "requirement cleared, instance active again")
        check(inst.context["completed_requirements"][-1]["resolution"]["reference"] == "chk-1041", "reference kept on the instance")
        await tick(rt)
        sent = drain(rt)
        check(any("chk-1041" in m.body for m in sent), "next message to the provider cites the payment reference")

        # -------------------------------------------------- 10. records received
        step("10", "Provider sends the records -> client notified, instance completed")
        await inbound(rt, iid, "Here are the records, sent over by fax this morning", channel="email")
        sent = drain(rt)
        inst = await instance(rt, iid)
        check(inst.status == "completed" and inst.state == "completed", "instance completed")
        check(sent[-1].address == "+15550001", "client told the records arrived")
        await timeline(rt, iid)

        # ------------------------------------------------ 11. the other workflow
        step("11", "Same engine, different workflow: client_checkin (zero engine changes)")
        async with rt.session_factory() as s, s.begin():
            inst2 = await rt.engine.start_instance(
                s, "client_checkin", case_id=case_id,
                context={"target_contact_id": str(client_id), "client_contact_id": str(client_id), "client_channel": "sms"},
            )
            iid2 = inst2.id
        drain(rt)
        await inbound(rt, iid2, "Doing fine, nothing new to report", channel="sms", who="client")
        inst2_row = await instance(rt, iid2)
        check(inst2_row.state == "scheduled" and inst2_row.wake_reason == "cadence", f"check-in rescheduled on its 14d cadence: {inst2_row.next_wake_at:%Y-%m-%d %H:%M}")
        rt.clock.advance(timedelta(days=15))
        clockline(rt)
        await tick(rt)
        drain(rt)
        await inbound(rt, iid2, "honestly the pain is getting worse and I can't sleep", channel="sms", who="client")
        drain(rt)
        inst2_row = await instance(rt, iid2)
        tasks2 = await pending_reviews(rt, iid2)
        check(inst2_row.status == "blocked", "concerning reply blocked the instance")
        check(bool(tasks2) and tasks2[0].reason.startswith("client_flag:"), f"review_task raised for staff: {tasks2[0].reason}")
        await timeline(rt, iid2)

        # -------------------------------------------------------- 12. client-side update
        step("12", "Client updates their own contact details mid-workflow")
        async with rt.session_factory() as s, s.begin():
            inst2_locked = await s.get(WorkflowInstance, iid2, with_for_update=True)
            await rt.engine.handle_inbound(s, iid2, "also my new email is jane.doe@newmail.example", channel="sms")
        async with rt.session_factory() as s:
            new_email = await get_entity_field(s, "contact", client_id, "email")
        check(new_email == "jane.doe@newmail.example", "client's email updated through the same generic write-back path")

        print(f"\n{BOLD}{GREEN}Demo complete - every assertion passed.{RESET}\n")
    finally:
        await rt.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", dest="db", default=None, help="Postgres URL (default: embedded pgserver)")
    ap.add_argument("--brain", choices=["rules", "gemini"], default=None, help="override AGENT_BRAIN for this run")
    args = ap.parse_args()
    if args.brain:
        os.environ["AGENT_BRAIN"] = args.brain
    configure_event_loop()  # must run BEFORE the loop exists (psycopg needs the selector loop on Windows)
    asyncio.run(main(args.db))
