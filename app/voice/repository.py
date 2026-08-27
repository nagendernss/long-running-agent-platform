"""Reading and writing calls.

The transcript is appended one turn at a time rather than written once at the end, so
a call that drops halfway - browser closed, laptop shut - still leaves everything that
was said behind, and the timeline can be watched live.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CallRow

# A call is `queued` when placed and `ringing` only while it is actually being
# offered, so the status says which it is rather than the caller having to know that
# a null ringing_since means nobody can hear it.
WAITING_STATUSES = ("queued", "ringing")
LIVE_STATUSES = ("queued", "ringing", "active")
FINAL_STATUSES = ("completed", "missed", "failed")


async def place_call(
    session: AsyncSession,
    *,
    instance_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    goal: str,
    opening: str,
    to_address: str | None,
    now: datetime,
) -> CallRow:
    call = CallRow(
        id=uuid.uuid4(),
        instance_id=instance_id,
        contact_id=contact_id,
        status="queued",
        goal=goal,
        opening=opening,
        to_address=to_address,
        transcript=[],
        created_at=now,
    )
    session.add(call)
    await session.flush()
    return call


async def get_call(session: AsyncSession, call_id: uuid.UUID) -> CallRow | None:
    return await session.get(CallRow, call_id)


async def waiting_calls(session: AsyncSession) -> list[CallRow]:
    """Everything placed and not yet answered - queued or ringing - oldest first."""
    stmt = (
        select(CallRow)
        .where(CallRow.status.in_(WAITING_STATUSES))
        # seq breaks the tie: two calls placed in the same instant - several instances
        # waking together, or a virtual clock - would otherwise queue in random order.
        .order_by(CallRow.created_at.asc(), CallRow.seq.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def ringing_call(session: AsyncSession) -> CallRow | None:
    """The one being offered right now. At most one, by construction."""
    stmt = select(CallRow).where(CallRow.status == "ringing").limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def live_call_for_instance(session: AsyncSession, instance_id: uuid.UUID) -> CallRow | None:
    """One call at a time per instance: a second would talk over the first."""
    stmt = (
        select(CallRow)
        .where(CallRow.instance_id == instance_id, CallRow.status.in_(LIVE_STATUSES))
        .order_by(CallRow.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def answer_call(session: AsyncSession, call_id: uuid.UUID, now: datetime) -> CallRow | None:
    """Valid from queued or ringing: the queue decides what to *offer*, not what a
    person is allowed to pick up. What it refuses is answering a call that is already
    active or finished."""
    call = await get_call(session, call_id)
    if call is None or call.status not in WAITING_STATUSES:
        return None
    call.status = "active"
    call.answered_at = now
    await session.flush()
    return call


async def append_turn(
    session: AsyncSession, call_id: uuid.UUID, who: str, text: str, now: datetime, source: str | None = None
) -> None:
    """Appended in SQL rather than read-modify-write, so two writes cannot lose a turn."""
    turn: dict[str, Any] = {"who": who, "text": text, "at": now.isoformat()}
    if source:
        turn["source"] = source
    await session.execute(
        update(CallRow)
        .where(CallRow.id == call_id)
        .values(transcript=CallRow.transcript.concat([turn]))
    )


async def finish_call(session: AsyncSession, call_id: uuid.UUID, status: str, now: datetime) -> CallRow | None:
    if status not in FINAL_STATUSES:
        raise ValueError(f"not a final call status: {status!r}")
    call = await get_call(session, call_id)
    if call is None or call.status in FINAL_STATUSES:
        return call  # already finished: a dropped socket and a hang-up can both arrive
    call.status = status
    call.ended_at = now
    await session.flush()
    return call


def transcript_text(call: CallRow) -> str:
    """What the Agent Brain reads. Only what the other party said is evidence - the
    agent's own words are context, and feeding them back would let the agent's guesses
    become extracted facts."""
    return " ".join(t.get("text", "") for t in (call.transcript or []) if t.get("who") == "contact").strip()
