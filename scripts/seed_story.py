"""Play a realistic story into the database so the dashboard opens on real history.

Runs on a VIRTUAL clock. The instance timeline groups events into attempts and draws
the wait between them, which only means something if the timestamps are genuinely days
apart - so the seeder time-travels even though the server itself runs on real time.

    python scripts/serve.py --embedded --reset --seed
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.channels import MockChannel
from app.clock import FakeClock
from app.config import Settings, get_settings
from app.db.models import CaseRecord, Contact, ReviewTask, WorkflowInstance
from app.db.session import make_engine
from app.runtime import build_runtime

TABLES = ("call, review_task, entity_fact_version, event, workflow_instance, "
          "case_record, contact, procrastinate_jobs")


async def reset_database(settings: Settings | None = None) -> None:
    """Drop every row; the schema stays. Procrastinate jobs go too, so no orphaned timers."""
    engine = make_engine((settings or get_settings()).sqlalchemy_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    finally:
        await engine.dispose()
    print("database flushed")


async def seed_story(settings: Settings | None = None) -> None:
    # Start ~26 days in the past so the story lands at "now": the history has real
    # weeks in it, and the wakes it leaves behind are genuinely in the future - which
    # is what makes the header's time-travel buttons mean anything.
    clock = FakeClock(datetime.now(timezone.utc) - timedelta(days=26))
    started = clock.now()
    rt = await build_runtime(settings or get_settings(), clock=clock, channel=MockChannel())

    async def inbound(instance_id, message, channel="call"):
        async with rt.session_factory() as s, s.begin():
            await rt.engine.handle_inbound(s, instance_id, message, channel=channel)

    async def run_due():
        async with rt.session_factory() as s, s.begin():
            await rt.engine.run_due(s)

    async def jump_to_next_wake(instance_id):
        async with rt.session_factory() as s:
            inst = await s.get(WorkflowInstance, instance_id)
        if inst and inst.next_wake_at:
            clock.set(inst.next_wake_at + timedelta(minutes=3))
            await run_due()

    async def pending_task(instance_id):
        async with rt.session_factory() as s:
            stmt = (
                select(ReviewTask)
                .where(ReviewTask.instance_id == instance_id, ReviewTask.status == "pending")
                .order_by(ReviewTask.created_at.desc())
                .limit(1)
            )
            return (await s.execute(stmt)).scalar_one_or_none()

    try:
        async with rt.session_factory() as s, s.begin():
            if (await s.execute(select(WorkflowInstance.id).limit(1))).scalar_one_or_none():
                print("database already has instances; skipping seed")
                return
            now = clock.now()
            client = Contact(id=uuid.uuid4(), name="Jane Okafor", role="client", phone="+15550001",
                             email="jane@example.com", created_at=now)
            provider = Contact(id=uuid.uuid4(), name="Mercy Hospital Records", role="provider",
                               phone="+15550100", email="records@mercy.example", timezone="America/Chicago",
                               business_hours={"start": "08:00", "end": "16:00"}, created_at=now)
            clerk = Contact(id=uuid.uuid4(), name="County Clerk", role="staff", phone="+15550300",
                            email="clerk@county.example", created_at=now)
            s.add_all([client, provider, clerk])
            await s.flush()
            case = CaseRecord(id=uuid.uuid4(), client_contact_id=client.id,
                              matter_type="personal_injury", created_at=now)
            s.add(case)
            await s.flush()
            client_id, provider_id, clerk_id, case_id = client.id, provider.id, clerk.id, case.id

        # 1. the long one: wrong number, a two-week delay, a fee, then escalation
        async with rt.session_factory() as s, s.begin():
            medical = await rt.engine.start_instance(
                s, "medical_records_followup", case_id=case_id,
                context={"target_contact_id": str(provider_id), "provider_contact_id": str(provider_id),
                         "client_contact_id": str(client_id), "provider_channel": "call", "client_channel": "sms"},
            )
            mid = medical.id

        clock.advance(timedelta(hours=6))
        await inbound(mid, "You've got the old number for us - it's actually 555-0199.")
        clock.advance(timedelta(days=1))
        await inbound(mid, "Records aren't available for about two weeks. Call us back then.")
        await jump_to_next_wake(mid)
        await inbound(mid, "No answer, went to voicemail.")
        await jump_to_next_wake(mid)
        await inbound(mid, "There is a $45 retrieval fee. Pay at pay.mercy.example before we release anything.")

        clock.advance(timedelta(days=1))
        fee = await pending_task(mid)
        if fee:
            async with rt.session_factory() as s, s.begin():
                await rt.engine.resolve_review_task(
                    s, fee.id, {"action": "paid", "reference": "chk-1041"}, resolved_by="paralegal@firm"
                )
            await run_due()

        clock.advance(timedelta(days=2))
        await inbound(mid, "No answer.")
        await jump_to_next_wake(mid)
        await inbound(mid, "Still no answer, voicemail again.")  # ladder runs out -> escalated

        # 2. a check-in sitting quietly on its cadence
        clock.advance(timedelta(days=1))
        async with rt.session_factory() as s, s.begin():
            checkin = await rt.engine.start_instance(
                s, "client_checkin", case_id=case_id,
                context={"target_contact_id": str(client_id), "client_contact_id": str(client_id),
                         "client_channel": "sms"},
            )
            cid = checkin.id
        clock.advance(timedelta(hours=3))
        await inbound(cid, "Doing okay, nothing new to report.", channel="sms")

        # 3. a one-shot update that landed first time
        clock.advance(timedelta(days=1))
        async with rt.session_factory() as s, s.begin():
            notice = await rt.engine.start_instance(
                s, "contact_update", case_id=case_id,
                context={"target_contact_id": str(clerk_id), "channel": "email",
                         "message": "Confirming the filing deadline for the Okafor matter is March 3rd."},
            )
            nid = notice.id
        clock.advance(timedelta(hours=20))
        await inbound(nid, "Received, thanks - that matches our docket.", channel="email")

        print(f"seeded 3 instances spanning {(clock.now() - started).days} virtual days")
    finally:
        await rt.close()


# The demo set: one instance per workflow, and what each one needs to run.
DEMO_FLOWS = ("medical_records_followup", "client_checkin", "contact_update")


async def _demo_cast(session, now):
    """The three contacts and the case, created once and reused on every later start.

    Matched by name rather than tracked by id, because the point is that restarting the
    server twice does not leave six Jane Okafors behind.
    """
    people = {}
    wanted = [
        dict(name="Jane Okafor", role="client", phone="+15550001", email="jane@example.com"),
        dict(name="Mercy Hospital Records", role="provider", phone="+15550100",
             email="records@mercy.example", timezone="America/Chicago",
             business_hours={"start": "08:00", "end": "16:00"}),
        dict(name="County Clerk", role="staff", phone="+15550300", email="clerk@county.example"),
    ]
    for spec in wanted:
        existing = (await session.execute(
            select(Contact).where(Contact.name == spec["name"]).limit(1)
        )).scalar_one_or_none()
        if existing is None:
            existing = Contact(id=uuid.uuid4(), created_at=now, **spec)
            session.add(existing)
        people[spec["role"]] = existing
    await session.flush()

    case = (await session.execute(
        select(CaseRecord).where(CaseRecord.client_contact_id == people["client"].id).limit(1)
    )).scalar_one_or_none()
    if case is None:
        case = CaseRecord(id=uuid.uuid4(), client_contact_id=people["client"].id,
                          matter_type="personal_injury", created_at=now)
        session.add(case)
        await session.flush()
    return people, case


def _demo_context(workflow_type: str, people, case) -> dict:
    client, provider, clerk = people["client"], people["provider"], people["staff"]
    return {
        "medical_records_followup": {
            "target_contact_id": str(provider.id), "provider_contact_id": str(provider.id),
            "client_contact_id": str(client.id), "provider_channel": "call", "client_channel": "sms",
        },
        "client_checkin": {
            "target_contact_id": str(client.id), "client_contact_id": str(client.id),
            "client_channel": "sms",
        },
        "contact_update": {
            "target_contact_id": str(clerk.id), "channel": "email",
            "message": "Confirming the filing deadline for the Okafor matter is March 3rd.",
        },
    }[workflow_type]


async def seed_fresh(settings: Settings | None = None) -> None:
    """Day zero: one *active* instance per demo workflow, started just now, no history.

    Tops up rather than refusing to run. It used to skip entirely if the database held
    any instance at all, which meant that once one of them completed - a reviewer
    closing it, a chase that ended - restarting could never get that flow back, and the
    dashboard opened on two live agents and a corpse. Now a workflow with no active
    instance gets a new one at its first attempt, and whatever already ran stays where
    it is: finished instances are history, not clutter to be cleaned up.

    Each new instance opens on its first attempt with a response deadline armed, so the
    header's "skip to next wake" walks it forward through the retry ladder from the start.
    """
    from app.channels import MockChannel

    rt = await build_runtime(settings or get_settings(), channel=MockChannel())
    try:
        async with rt.session_factory() as s, s.begin():
            now = rt.clock.now()
            live = set((await s.execute(
                select(WorkflowInstance.workflow_type).where(WorkflowInstance.status != "completed")
            )).scalars().all())
            missing = [wf for wf in DEMO_FLOWS if wf not in live]
            if not missing:
                print(f"{len(DEMO_FLOWS)} flows already running; nothing to start")
                return

            people, case = await _demo_cast(s, now)
            for workflow_type in missing:
                await rt.engine.start_instance(
                    s, workflow_type, case_id=case.id, context=_demo_context(workflow_type, people, case)
                )
        started = ", ".join(missing)
        print(f"started {len(missing)} instance(s) at day zero: {started}")
    finally:
        await rt.close()
