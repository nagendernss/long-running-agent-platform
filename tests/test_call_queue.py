"""One phone, one person: several calls coming due together must queue, not collide."""
from __future__ import annotations

from datetime import timedelta

from app.voice.queue import RING_TIMEOUT, offer_next, sweep_calls
from app.voice.repository import answer_call, finish_call, place_call
from tests.helpers import load


async def place(rt, seed, instance_id=None, name="Mercy"):
    async with rt.session_factory() as s, s.begin():
        call = await place_call(
            s, instance_id=instance_id, contact_id=seed.provider_id,
            goal=f"call {name}", opening=f"Hi {name}", to_address="+15550100", now=rt.clock.now(),
        )
        return call.id


async def state(rt):
    async with rt.session_factory() as s, s.begin():
        result = await offer_next(s, rt.clock)
        return (result.offered.id if result.offered else None), result.waiting, result.busy


async def test_calls_are_offered_one_at_a_time_oldest_first(rt, seed):
    first = await place(rt, seed, name="one")
    rt.clock.advance(timedelta(seconds=5))
    second = await place(rt, seed, name="two")
    rt.clock.advance(timedelta(seconds=5))
    third = await place(rt, seed, name="three")

    offered, waiting, busy = await state(rt)
    assert offered == first and waiting == 2 and busy is False

    async with rt.session_factory() as s, s.begin():
        await finish_call(s, first, "completed", rt.clock.now())
    offered, waiting, _ = await state(rt)
    assert offered == second and waiting == 1

    async with rt.session_factory() as s, s.begin():
        await finish_call(s, second, "completed", rt.clock.now())
    offered, waiting, _ = await state(rt)
    assert offered == third and waiting == 0


async def test_nothing_is_offered_while_a_call_is_live(rt, seed):
    """The line is busy - a second call must not ring in someone's ear mid-conversation."""
    first = await place(rt, seed, name="one")
    await place(rt, seed, name="two")

    async with rt.session_factory() as s, s.begin():
        await offer_next(s, rt.clock)          # queued -> ringing
        await answer_call(s, first, rt.clock.now())

    offered, waiting, busy = await state(rt)
    assert offered is None and busy is True and waiting == 1


async def test_the_ring_timeout_starts_when_a_call_is_offered_not_when_placed(rt, seed):
    """A call waiting behind a long conversation has not been ignored; timing it out
    for that would turn a busy hour into a pile of false no-answers."""
    first = await place(rt, seed, name="one")
    second = await place(rt, seed, name="two")

    async with rt.session_factory() as s, s.begin():
        await offer_next(s, rt.clock)          # first is offered now
    rt.clock.advance(RING_TIMEOUT * 3)          # a long call happens

    async with rt.session_factory() as s, s.begin():
        await finish_call(s, first, "completed", rt.clock.now())
        offered = (await offer_next(s, rt.clock)).offered
    assert offered.id == second
    assert offered.ringing_since == rt.clock.now(), "its clock starts now, not when it was placed"


async def test_a_call_that_rings_out_becomes_a_no_answer(rt, seed):
    """And the retry ladder picks it up, with no special case for voice."""
    from tests.helpers import medical_context

    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        iid = inst.id
    call_id = await place(rt, seed, instance_id=iid)

    async with rt.session_factory() as s, s.begin():
        await offer_next(s, rt.clock)
    result = await sweep_calls(rt.session_factory, rt.clock, rt.engine)
    assert result["timed_out"] is None, "not yet - it has only just started ringing"

    rt.clock.advance(RING_TIMEOUT + timedelta(seconds=1))
    result = await sweep_calls(rt.session_factory, rt.clock, rt.engine)
    assert result["timed_out"] == str(call_id)

    inst = await load(rt, iid)
    assert inst.attempt_count == 1 and inst.wake_reason == "retry"


async def test_ringing_out_brings_the_next_call_forward_at_once(rt, seed):
    first = await place(rt, seed, name="one")
    second = await place(rt, seed, name="two")

    async with rt.session_factory() as s, s.begin():
        await offer_next(s, rt.clock)
    rt.clock.advance(RING_TIMEOUT + timedelta(seconds=1))
    await sweep_calls(rt.session_factory, rt.clock, rt.engine)

    offered, waiting, _ = await state(rt)
    assert offered == second and waiting == 0, "the next one is ringing now"
    assert offered != first


async def test_the_sweep_leaves_a_live_call_alone(rt, seed):
    call_id = await place(rt, seed)
    async with rt.session_factory() as s, s.begin():
        await offer_next(s, rt.clock)
        await answer_call(s, call_id, rt.clock.now())
    rt.clock.advance(RING_TIMEOUT * 5)

    result = await sweep_calls(rt.session_factory, rt.clock, rt.engine)
    assert result["timed_out"] is None and result["busy"] is True


async def test_the_sweep_is_registered_to_run_on_its_own(rt):
    from app.scheduling.scheduler import CALLS_CRON, CALLS_TASK_NAME

    periodic = {p.task.name: p.cron for p in rt.procrastinate_app.periodic_registry.periodic_tasks.values()}
    assert periodic.get(CALLS_TASK_NAME) == CALLS_CRON, "a queue nothing drains is a queue that stalls"


async def test_a_call_rings_for_a_minute(rt, seed):
    """Long enough for someone to reach the phone, short enough that a queue behind it
    is not held up by a call nobody is going to take."""
    assert RING_TIMEOUT == timedelta(seconds=60)

    call_id = await place(rt, seed)
    async with rt.session_factory() as s, s.begin():
        await offer_next(s, rt.clock)

    rt.clock.advance(timedelta(seconds=59))
    assert (await sweep_calls(rt.session_factory, rt.clock, rt.engine))["timed_out"] is None

    rt.clock.advance(timedelta(seconds=2))
    assert (await sweep_calls(rt.session_factory, rt.clock, rt.engine))["timed_out"] == str(call_id)


async def test_queued_and_ringing_are_different_things(rt, seed):
    """`ringing` used to be set the moment a call was placed, so it covered both a call
    nobody could hear and the one actually being offered. The status now says which."""
    from app.voice.repository import ringing_call, waiting_calls

    first = await place(rt, seed, name="one")
    second = await place(rt, seed, name="two")

    async with rt.session_factory() as s:
        assert [c.status for c in await waiting_calls(s)] == ["queued", "queued"]
        assert await ringing_call(s) is None, "nothing is ringing until it is offered"

    async with rt.session_factory() as s, s.begin():
        await offer_next(s, rt.clock)

    async with rt.session_factory() as s:
        statuses = {c.id: c.status for c in await waiting_calls(s)}
        assert statuses[first] == "ringing" and statuses[second] == "queued"
        assert (await ringing_call(s)).id == first, "exactly one is ringing"
