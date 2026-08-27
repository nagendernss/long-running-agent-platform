"""A person's verdict on a fact the extraction was not sure enough to apply.

Two holes, found from opposite ends of the same feature. The engine escalated a
low-confidence fact with `suggested_options=["apply", "reject"]` and stashed the
fact id - and then nothing rendered those options, and nothing implemented them
either. What the review queue *did* offer was "retry" and "close", and "close"
completes the whole instance: a reviewer answering "no, don't trust that email"
could end a two-week records chase.

So: apply and reject do what they say, and neither of them touches the workflow.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.facts import get_entity_field
from app.db.models import EntityFactVersion
from app.engine import RETRY_AFTER_CORRECTION
from app.signals import EntityUpdate
from tests.helpers import events, load, medical_context, review_tasks


async def start(rt, seed):
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        return inst.id


async def unsure_correction(rt, seed, iid, *, field="phone", value="5550199", confidence=0.4):
    """Heard something, but not confidently enough to use it."""
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.advance_instance(s, inst, [EntityUpdate(
            entity_type="contact", entity_id=str(seed.provider_id), field=field,
            new_value=value, confidence=confidence, evidence="half heard on a call",
        )])
    tasks = await review_tasks(rt, iid)
    return next(t for t in tasks if t.reason == "low_confidence_fact")


async def resolve(rt, task_id, action):
    async with rt.session_factory() as s, s.begin():
        await rt.engine.resolve_review_task(s, task_id, {"action": action}, resolved_by="dashboard")


async def fact_rows(rt, seed, field="phone"):
    async with rt.session_factory() as s:
        stmt = (select(EntityFactVersion)
                .where(EntityFactVersion.entity_id == seed.provider_id, EntityFactVersion.field == field)
                .order_by(EntityFactVersion.created_at))
        return list((await s.execute(stmt)).scalars().all())


async def current_phone(rt, seed):
    async with rt.session_factory() as s:
        return await get_entity_field(s, "contact", seed.provider_id, "phone")


# -- apply -------------------------------------------------------------------------

async def test_applying_a_proposed_fact_makes_it_the_value_we_use(rt, seed):
    iid = await start(rt, seed)
    task = await unsure_correction(rt, seed, iid)
    assert await current_phone(rt, seed) != "5550199", "not used while it is only proposed"

    await resolve(rt, task.id, "apply")

    assert [f.status for f in await fact_rows(rt, seed)] == ["applied"]
    assert await current_phone(rt, seed) == "5550199"
    assert "fact_applied" in await events(rt, iid)


async def test_a_confirmed_number_is_dialled_now_not_in_three_days(rt, seed):
    """A person confirming how to reach someone is worth exactly what a confident
    extraction is worth - including the short retry."""
    iid = await start(rt, seed)
    task = await unsure_correction(rt, seed, iid)
    before = await load(rt, iid)

    await resolve(rt, task.id, "apply")

    inst = await load(rt, iid)
    assert inst.status == "active", "the last review task cleared"
    assert inst.wake_reason == "retry_after_correction"
    assert inst.next_wake_at < before.created_at + RETRY_AFTER_CORRECTION * 3


async def test_confirming_something_that_is_not_reachability_does_not_redial(rt, seed):
    """Only a way to reach them is worth acting on immediately."""
    iid = await start(rt, seed)
    task = await unsure_correction(rt, seed, iid, field="preferred_contact_time", value="mornings")
    await resolve(rt, task.id, "apply")

    assert (await load(rt, iid)).wake_reason != "retry_after_correction"


# -- reject ------------------------------------------------------------------------

async def test_rejecting_keeps_the_old_value_and_keeps_the_row(rt, seed):
    """A wrong extraction is evidence too: the row stays, it just can never win."""
    iid = await start(rt, seed)
    task = await unsure_correction(rt, seed, iid)

    await resolve(rt, task.id, "reject")

    rows = await fact_rows(rt, seed)
    assert [f.status for f in rows] == ["rejected"] and rows[0].new_value == "5550199"
    assert await current_phone(rt, seed) != "5550199"


# -- the trap ----------------------------------------------------------------------

async def test_neither_verdict_ends_the_instance(rt, seed):
    """The bug this file exists for. A verdict on one value is not a decision to stop
    chasing, and `close` used to be the only button the queue actually offered."""
    for action in ("apply", "reject"):
        iid = await start(rt, seed)
        task = await unsure_correction(rt, seed, iid)
        await resolve(rt, task.id, action)

        inst = await load(rt, iid)
        assert inst.status == "active", f"{action} left the instance running"
        assert inst.state != "completed"
        assert "completed" not in await events(rt, iid)


async def test_a_second_verdict_changes_nothing(rt, seed):
    """Two people, one queue, one back button."""
    iid = await start(rt, seed)
    task = await unsure_correction(rt, seed, iid)
    await resolve(rt, task.id, "apply")
    await resolve(rt, task.id, "reject")

    assert [f.status for f in await fact_rows(rt, seed)] == ["applied"]


async def test_a_newer_correction_still_wins_after_an_older_one_is_approved(rt, seed):
    """Approving a guess that sat in the queue must not un-learn what was heard since."""
    iid = await start(rt, seed)
    task = await unsure_correction(rt, seed, iid, value="5550199")

    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.advance_instance(s, inst, [EntityUpdate(
            entity_type="contact", entity_id=str(seed.provider_id), field="phone",
            new_value="5550200", confidence=0.99, evidence="spelled out digit by digit",
        )])

    await resolve(rt, task.id, "apply")
    assert await current_phone(rt, seed) == "5550200"
