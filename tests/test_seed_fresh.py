"""The dev server should open on three running agents, every time.

`seed_fresh` used to be all-or-nothing: it refused to do anything if the database held
a single instance. So the first time one of the three finished - a reviewer closing it,
a chase that ended - restarting could never bring that flow back, and the dashboard
opened on two live agents and a corpse. It tops up now.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import func, select

from app.db.models import Contact, WorkflowInstance

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from seed_story import DEMO_FLOWS, seed_fresh  # noqa: E402


async def instances(rt) -> list[WorkflowInstance]:
    async with rt.session_factory() as s:
        stmt = select(WorkflowInstance).order_by(WorkflowInstance.created_at)
        return list((await s.execute(stmt)).scalars().all())


async def contact_count(rt) -> int:
    async with rt.session_factory() as s:
        return await s.scalar(select(func.count()).select_from(Contact))


async def test_a_clean_database_opens_on_three_running_agents(rt, settings):
    await seed_fresh(settings)

    rows = await instances(rt)
    assert sorted(i.workflow_type for i in rows) == sorted(DEMO_FLOWS)
    assert {i.status for i in rows} == {"active"}
    assert all(i.next_wake_at is not None for i in rows), "each one has its next attempt armed"


async def test_starting_twice_does_not_double_anything(rt, settings):
    await seed_fresh(settings)
    await seed_fresh(settings)

    assert len(await instances(rt)) == len(DEMO_FLOWS)
    assert await contact_count(rt) == 3, "no second Jane Okafor"


async def test_a_finished_flow_comes_back_at_day_zero(rt, settings):
    """The reported case: the records chase was closed by staff, and every restart
    afterwards left it finished. What is wanted is a new one at its first attempt -
    with the old one still on file, because a finished instance is history."""
    await seed_fresh(settings)
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(
            s, next(i.id for i in await instances(rt) if i.workflow_type == "medical_records_followup")
        )
        await rt.engine.ctx(s).complete(inst, outcome="closed_by_staff")
    finished = next(i for i in await instances(rt) if i.status == "completed")

    await seed_fresh(settings)

    rows = await instances(rt)
    live = [i for i in rows if i.status == "active"]
    assert sorted(i.workflow_type for i in live) == sorted(DEMO_FLOWS), "all three running again"
    assert len(rows) == len(DEMO_FLOWS) + 1, "the closed one is kept, not cleaned up"

    replacement = next(i for i in live if i.workflow_type == "medical_records_followup")
    assert replacement.id != finished.id and replacement.attempt_count == 0
    assert await contact_count(rt) == 3, "it reused the cast"


async def test_a_blocked_flow_is_not_replaced(rt, settings):
    """Blocked means a person is dealing with it. That is still a flow in play, and
    starting a second one alongside would have two agents chasing the same contact."""
    from app.signals import NeedsHuman

    await seed_fresh(settings)
    target = next(i for i in await instances(rt) if i.workflow_type == "client_checkin")
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, target.id)
        await rt.engine.advance_instance(s, inst, [NeedsHuman(reason="caller_requested_human")])

    await seed_fresh(settings)

    rows = await instances(rt)
    assert len(rows) == len(DEMO_FLOWS)
    assert next(i for i in rows if i.workflow_type == "client_checkin").status == "blocked"
