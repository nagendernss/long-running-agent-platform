"""The channel that places a real call instead of pretending to send one.

The important property is that `send` **returns immediately**. It is called inside the
engine's transaction, and a call lasts minutes: holding that transaction open for the
length of a conversation would pin a database connection, block every other path that
touches the instance, and undo the reason wakes are short. So this writes a `ringing`
row and returns; the conversation happens on its own, and its transcript comes back
through `handle_inbound` exactly as a typed reply does.

Anything that is not a call is delegated to a wrapped channel, so SMS and email keep
working untouched.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Channel, OutboundMessage
from app.clock import Clock
from app.voice.repository import live_call_for_instance, place_call

log = logging.getLogger(__name__)


def build_goal(definition, instance_context: dict | None = None) -> str:
    """What the agent is trying to achieve, assembled from what the workflow already
    declares - its description and the docstrings of the signals it can act on. A
    workflow that can handle a fee has already written down what a fee looks like."""
    parts: list[str] = []
    spec = getattr(definition, "spec", None)
    described = getattr(spec, "description", None) or (definition.__doc__ or "").strip().splitlines()
    if isinstance(described, str):
        parts.append(described)
    elif described:
        parts.append(described[0])

    outcomes = []
    for signal in getattr(definition, "domain_signals", []) or []:
        doc = " ".join((signal.__doc__ or "").split())
        if doc:
            outcomes.append(f"- {signal.model_fields['type'].default}: {doc}")
    if outcomes:
        parts.append("Find out whether any of these apply, and capture the particulars:\n" + "\n".join(outcomes))
    parts.append(
        "Also capture anything they correct about how to reach them, anything they need from us first "
        "(a fee, a form, a portal), and when to call back if they ask for that."
    )
    return "\n\n".join(p for p in parts if p)


class VoiceChannel:
    """Places calls; delegates every other channel to `fallback`."""

    name = "voice"

    def __init__(self, fallback: Channel, clock: Clock, registry=None):
        self.fallback = fallback
        self.clock = clock
        self.registry = registry

    async def send(self, session: AsyncSession, message: OutboundMessage) -> str:
        if message.channel != "call":
            return await self.fallback.send(session, message)

        existing = await live_call_for_instance(session, message.instance_id)
        if existing is not None:
            log.info("instance %s already has a live call; not placing another", message.instance_id)
            return str(existing.id)

        goal = await self._goal_for(session, message)
        call = await place_call(
            session,
            instance_id=message.instance_id,
            contact_id=message.contact_id,
            goal=goal,
            opening=message.body,
            to_address=message.address,
            now=self.clock.now(),
        )
        log.info("placed call %s to %s", call.id, message.address)
        return str(call.id)

    async def _goal_for(self, session: AsyncSession, message: OutboundMessage) -> str:
        """The workflow already declares what it is trying to achieve; the engine has
        this instance loaded in the same session, so reading it back is an identity-map
        hit rather than a query."""
        from app.db.models import WorkflowInstance

        if self.registry is not None:
            instance = await session.get(WorkflowInstance, message.instance_id)
            if instance is not None:
                try:
                    return build_goal(self.registry.get(instance.workflow_type))
                except KeyError:
                    log.warning("no definition for %s; using a generic goal", instance.workflow_type)
        return "Find out what they can tell us, and capture anything they need from us first."
