"""One call at a time.

A single person answers the phone, so several instances coming due together must not
all ring at once - they queue, oldest first, and the next is offered only when the
line is free.

Two rules keep that honest:

* **The ring timeout starts when a call is actually offered**, not when it was placed.
  A call waiting behind a twenty-minute conversation has not been ignored, and timing
  it out for that would turn a busy hour into a pile of false no-answers.
* **A call nobody picks up is a no-answer**, routed through the same door as any
  other, so it reaches the retry ladder rather than a special case.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.clock import Clock
from app.db.models import CallRow
from app.voice.session import miss_call

log = logging.getLogger(__name__)

# A minute of ringing is as long as anyone waits. After that the call is a no-answer
# and the next one in the queue gets its turn.
RING_TIMEOUT = timedelta(seconds=int(os.environ.get("CALL_RING_TIMEOUT_SECONDS", "60")))


@dataclass
class QueueState:
    offered: CallRow | None
    waiting: int
    busy: bool


async def _oldest(session, *statuses: str) -> CallRow | None:
    stmt = (
        select(CallRow)
        .where(CallRow.status.in_(statuses))
        .order_by(CallRow.created_at.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def offer_next(session, clock: Clock) -> QueueState:
    """Which call the phone should be ringing about, if any.

    Stamps `ringing_since` when a call first reaches the front, which is what the
    timeout counts from. Returns nothing while a call is active: the line is busy.
    """
    active = await _oldest(session, "active")
    ringing = list(
        (await session.execute(
            select(CallRow).where(CallRow.status == "ringing").order_by(CallRow.created_at.asc())
        )).scalars().all()
    )
    if active is not None:
        return QueueState(offered=None, waiting=len(ringing), busy=True)
    if not ringing:
        return QueueState(offered=None, waiting=0, busy=False)

    head = ringing[0]
    if head.ringing_since is None:
        head.ringing_since = clock.now()
        await session.flush()
        log.info("offering call %s (%s waiting behind it)", head.id, len(ringing) - 1)
    return QueueState(offered=head, waiting=len(ringing) - 1, busy=False)


async def sweep_calls(session_factory, clock: Clock, engine, ring_timeout: timedelta = RING_TIMEOUT) -> dict:
    """Periodic: time out the call currently being offered, then offer the next.

    Called on a cron so a queue drains on its own even if nobody is looking at the
    phone page - otherwise an unanswered call would sit ringing forever and the
    instances behind it would never be attempted.
    """
    async with session_factory() as session, session.begin():
        state = await offer_next(session, clock)
        timed_out = (
            state.offered is not None
            and state.offered.ringing_since is not None
            and clock.now() - state.offered.ringing_since >= ring_timeout
        )
        call_id = state.offered.id if state.offered else None
        waiting = state.waiting

    if timed_out and call_id is not None:
        log.info("call %s rang out after %s", call_id, ring_timeout)
        await miss_call(session_factory, clock, engine, call_id, reason="rang out")
        async with session_factory() as session, session.begin():
            await offer_next(session, clock)   # bring the next one forward at once
        return {"timed_out": str(call_id), "waiting": waiting}

    return {"timed_out": None, "waiting": waiting, "busy": state.busy}
