"""Engine: workflow-agnostic orchestrator.

Owns: instance lifecycle, generic-signal handling (RESCHEDULE / NO_ANSWER /
ENTITY_UPDATE / ACTION_REQUIRED / NEEDS_HUMAN), wake execution (idempotent,
fenced), the human-in-the-loop hand-back, and the `WorkflowContext` capability
surface handed to workflow definitions.

Rule: nothing in this file knows about any concrete workflow. Adding a workflow
must not touch it.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_brain import AgentBrain
from app.channels import CHANNEL_ADDRESS_FIELD, Channel, OutboundMessage
from app.clock import Clock, parse_duration
from app.db.facts import get_entity_field
from app.db.models import Contact, EntityFactVersion, Event, ReviewTask, WorkflowInstance
from app.events import idempotency_key_exists, log_event
from app.review import create_review_task
from app.scheduling.constraints import apply_scheduling_constraints
from app.scheduling.scheduler import Scheduler
from app.signals import (
    GENERIC_SIGNAL_TYPES,
    REQUIREMENT_DONE_ACTIONS,
    ActionRequired,
    EntityUpdate,
    NeedsHuman,
    NoAnswer,
    Reschedule,
    Signal,
)
from app.retry import resolve_retry_delay
from app.workflows.base import WorkflowDefinition
from app.workflows.registry import WorkflowRegistry
from app.write_back import WriteBackResolver

log = logging.getLogger(__name__)

# Fields that change *how we reach them*. Correcting one means the attempt currently
# being waited on went to the wrong place, so it is worth trying again promptly.
REACHABILITY_FIELDS = frozenset({"phone", "email", "preferred_channel"})
RETRY_AFTER_CORRECTION = timedelta(minutes=int(os.environ.get("RETRY_AFTER_CORRECTION_MINUTES", "5")))


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class EngineContext:
    """Concrete WorkflowContext. One per unit of work (session)."""

    def __init__(self, engine: "Engine", session: AsyncSession):
        self.engine = engine
        self.session = session

    @property
    def now(self) -> datetime:
        return self.engine.clock.now()

    async def send(
        self, instance: WorkflowInstance, contact_id: uuid.UUID | str, body: str, channel: str = "sms"
    ) -> str:
        cid = _uuid(contact_id)
        channel = await self._resolve_channel(instance, cid, channel)
        field = CHANNEL_ADDRESS_FIELD[channel]
        # Always resolved through the fact store -> corrected numbers are picked up automatically.
        address = await get_entity_field(self.session, "contact", cid, field)
        if not address:
            await self.log(
                instance, "message_failed", {"contact_id": str(cid), "channel": channel, "reason": f"no {field} on file"}
            )
            return ""
        message = OutboundMessage(instance_id=instance.id, contact_id=cid, channel=channel, address=address, body=body)
        external_id = await self.engine.channel.send(self.session, message)
        await self.log(
            instance,
            "message_sent",
            {"contact_id": str(cid), "channel": channel, "to": address, "body": body, "external_id": external_id},
        )
        return external_id

    async def _resolve_channel(self, instance: WorkflowInstance, contact_id: uuid.UUID, requested: str) -> str:
        """A contact who said "don't call, email us" has a `preferred_channel` fact.
        Honour it for every send, so "reach me another way" needs no workflow change.
        Falls back to the requested channel if the preferred one has no address."""
        preferred = await get_entity_field(self.session, "contact", contact_id, "preferred_channel")
        if not preferred or preferred == requested or preferred not in CHANNEL_ADDRESS_FIELD:
            return requested
        if not await get_entity_field(self.session, "contact", contact_id, CHANNEL_ADDRESS_FIELD[preferred]):
            return requested
        await self.log(
            instance, "channel_overridden",
            {"contact_id": str(contact_id), "requested": requested, "used": preferred, "source": "preferred_channel fact"},
        )
        return preferred

    async def schedule_wake(self, instance: WorkflowInstance, at: datetime, reason: str) -> None:
        await self.engine.scheduler.schedule_wake(self.session, instance, at, reason)

    async def schedule_wake_in(self, instance: WorkflowInstance, duration: str, reason: str) -> None:
        contact = await self.engine._target_contact(self.session, instance)
        at = apply_scheduling_constraints(self.now + parse_duration(duration), contact, key=str(instance.id))
        await self.schedule_wake(instance, at, reason)

    async def transition(self, instance: WorkflowInstance, new_state: str) -> None:
        old = instance.state
        if old == new_state:
            return
        instance.state = new_state
        instance.updated_at = self.now
        await self.log(instance, "state_changed", {"from": old, "to": new_state})
        await self.engine.definition_for(instance).on_enter_state(instance, self)

    async def complete(self, instance: WorkflowInstance, outcome: str) -> None:
        await self.engine.scheduler.clear_wake(instance)
        await self.transition(instance, "completed")
        instance.status = "completed"
        instance.updated_at = self.now
        await self.log(instance, "completed", {"outcome": outcome})

    async def create_review_task(
        self,
        instance: WorkflowInstance,
        reason: str,
        suggested_options: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ReviewTask:
        return await create_review_task(
            self.session, instance, reason, now=self.now, suggested_options=suggested_options, extra=extra
        )

    async def log(self, instance: WorkflowInstance, type: str, payload: dict[str, Any]) -> Event:
        return await log_event(self.session, instance.id, type, payload, now=self.now)

    async def contact_field(self, contact_id: uuid.UUID | str, field: str) -> str | None:
        return await get_entity_field(self.session, "contact", _uuid(contact_id), field)

    async def contact_name(self, contact_id: uuid.UUID | str) -> str:
        contact = await self.session.get(Contact, _uuid(contact_id))
        return contact.name if contact else "unknown"


class Engine:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        scheduler: Scheduler,
        channel: Channel,
        brain: AgentBrain,
        registry: WorkflowRegistry,
        write_back: WriteBackResolver,
    ):
        self.session_factory = session_factory
        self.clock = clock
        self.scheduler = scheduler
        self.channel = channel
        self.brain = brain
        self.registry = registry
        self.write_back = write_back

    # -- helpers -------------------------------------------------------------------
    def ctx(self, session: AsyncSession) -> EngineContext:
        return EngineContext(self, session)

    def definition_for(self, instance: WorkflowInstance) -> WorkflowDefinition:
        return self.registry.get(instance.workflow_type)

    async def _lock_instance(self, session: AsyncSession, instance_id: uuid.UUID) -> WorkflowInstance | None:
        stmt = select(WorkflowInstance).where(WorkflowInstance.id == instance_id).with_for_update()
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _target_contact(self, session: AsyncSession, instance: WorkflowInstance) -> Contact | None:
        cid = instance.context.get("target_contact_id")
        return await session.get(Contact, _uuid(cid)) if cid else None

    async def _brain_context(
        self, session: AsyncSession, instance: WorkflowInstance, channel: str = "sms"
    ) -> dict[str, Any]:
        """What the brain is told about the conversation it is reading.

        Who is speaking changes what the words mean: "the pain is worse" from a client
        is something a person must see, while the same sentence from a provider's
        records desk is usually about a patient, not the sender.
        """
        contact = await self._target_contact(session, instance)
        return {
            "workflow_type": instance.workflow_type,
            "instance_id": str(instance.id),
            "state": instance.state,
            "context": instance.context,
            "target_contact_id": instance.context.get("target_contact_id"),
            "target_contact_name": contact.name if contact else None,
            "target_contact_role": contact.role if contact else None,
            "channel": channel,
        }

    # -- lifecycle -----------------------------------------------------------------
    async def start_instance(
        self,
        session: AsyncSession,
        workflow_type: str,
        *,
        case_id: uuid.UUID | None,
        context: dict[str, Any],
    ) -> WorkflowInstance:
        await self.registry.ensure_fresh(session)
        definition = self.registry.get(workflow_type)
        now = self.clock.now()
        instance = WorkflowInstance(
            id=uuid.uuid4(),
            workflow_type=workflow_type,
            case_id=case_id,
            state=definition.initial_state,
            context=dict(context),
            status="active",
            attempt_count=0,
            created_at=now,
            updated_at=now,
        )
        session.add(instance)
        await session.flush()
        await log_event(session, instance.id, "instance_created", {"workflow_type": workflow_type, "context": context}, now=now)
        await self._run_attempt(session, instance, key=f"{instance.id}:initial", reason="initial_send", start=True)
        return instance

    async def _run_attempt(
        self, session: AsyncSession, instance: WorkflowInstance, *, key: str, reason: str, start: bool = False
    ) -> bool:
        """Idempotent attempt execution: `attempt_started` is written (with a unique key)
        before any side effect, all inside the caller's transaction."""
        if await idempotency_key_exists(session, key):
            await log_event(session, instance.id, "wake_skipped", {"reason": "duplicate", "key": key}, now=self.clock.now())
            return False
        await log_event(
            session,
            instance.id,
            "attempt_started",
            {"attempt": instance.attempt_count, "reason": reason},
            now=self.clock.now(),
            idempotency_key=key,
        )
        definition = self.definition_for(instance)
        ctx = self.ctx(session)
        if start:
            await definition.on_start(instance, ctx)
        else:
            await definition.on_wake(instance, ctx)
        await self._arm_response_deadline(session, instance, definition)
        instance.updated_at = self.clock.now()
        return True

    async def _arm_response_deadline(
        self, session: AsyncSession, instance: WorkflowInstance, definition: WorkflowDefinition
    ) -> None:
        """After reaching out, wait a declared amount for a reply. Skipped when the
        workflow already scheduled something itself (a reminder, a cadence) or when it
        finished or handed off - so it never fights the workflow for the timer."""
        deadline = getattr(definition, "response_deadline", None)
        if not deadline or instance.status != "active" or instance.next_wake_at is not None:
            return
        at = self.clock.now() + parse_duration(deadline)
        await self.scheduler.schedule_wake(session, instance, at, reason="response_timeout")

    # -- wake execution (called by the Procrastinate worker AND the poller) ---------
    async def execute_wakeup(
        self, session: AsyncSession, instance_id: uuid.UUID, wake_token: str | None = None
    ) -> str:
        instance = await self._lock_instance(session, instance_id)
        now = self.clock.now()
        if instance is None:
            return "skipped:not_found"
        if instance.status != "active":
            await log_event(session, instance.id, "wake_skipped", {"reason": f"status={instance.status}"}, now=now)
            return f"skipped:status={instance.status}"
        if instance.wake_token is None or instance.next_wake_at is None:
            await log_event(session, instance.id, "wake_skipped", {"reason": "nothing_scheduled"}, now=now)
            return "skipped:nothing_scheduled"
        if wake_token is not None and wake_token != instance.wake_token:
            await log_event(
                session, instance.id, "wake_skipped", {"reason": "stale_token", "token": wake_token}, now=now
            )
            return "skipped:stale_token"

        key = f"{instance.id}:{instance.wake_token}"
        reason = instance.wake_reason or "wake"
        await self.scheduler.clear_wake(instance)

        if reason == "response_timeout":
            # Nobody replied in time. No outreach to make here - the silence itself is
            # the outcome, so it goes through the normal NO_ANSWER path (retry ladder,
            # client update, escalation when the ladder is spent).
            if await idempotency_key_exists(session, key):
                await log_event(session, instance.id, "wake_skipped", {"reason": "duplicate", "key": key}, now=now)
                return "skipped:duplicate"
            await log_event(
                session, instance.id, "response_timeout",
                {"attempt": instance.attempt_count, "waited": self.definition_for(instance).response_deadline},
                now=now, idempotency_key=key,
            )
            await self.advance_instance(
                session, instance,
                [NoAnswer(confidence=1.0, evidence="no reply before the response deadline")],
            )
            return "executed"

        ran = await self._run_attempt(session, instance, key=key, reason=reason)
        return "executed" if ran else "skipped:duplicate"

    async def run_wakeup(self, instance_id: uuid.UUID, wake_token: str | None) -> str:
        """Own-session variant for the Procrastinate task."""
        async with self.session_factory() as session:
            async with session.begin():
                return await self.execute_wakeup(session, instance_id, wake_token)

    async def run_due(self, session: AsyncSession, now: datetime | None = None) -> list[tuple[uuid.UUID, str]]:
        """Poller: execute every active instance whose next_wake_at <= now. Used by the
        demo/tests (virtual clock) and usable as a recovery sweep in production."""
        results = []
        for instance_id in await self.scheduler.due_instance_ids(session, now):
            results.append((instance_id, await self.execute_wakeup(session, instance_id)))
        return results

    # -- inbound -------------------------------------------------------------------
    async def handle_inbound(
        self, session: AsyncSession, instance_id: uuid.UUID, text: str, channel: str = "sms"
    ) -> list[Signal]:
        instance = await self._lock_instance(session, instance_id)
        if instance is None:
            raise KeyError(f"instance not found: {instance_id}")
        now = self.clock.now()
        if instance.status == "completed":
            await log_event(session, instance.id, "inbound_ignored", {"text": text, "reason": "completed"}, now=now)
            return []
        inbound = await log_event(session, instance.id, "inbound_received", {"text": text, "channel": channel}, now=now)
        signals = await self.brain.extract_signals(
            text, await self._brain_context(session, instance, channel)
        )
        if not signals:
            signals = [NeedsHuman(reason="unrecognized_reply", suggested_options=["read transcript"], evidence=text)]
        await log_event(
            session, instance.id, "signals_extracted", {"signals": [s.model_dump() for s in signals]}, now=now
        )
        await self.advance_instance(session, instance, signals, source_event_id=inbound.id)
        return signals

    # -- signal dispatcher ---------------------------------------------------------
    async def advance_instance(
        self,
        session: AsyncSession,
        instance: WorkflowInstance,
        signals: list[Signal],
        *,
        source_event_id: uuid.UUID | None = None,
    ) -> None:
        definition = self.definition_for(instance)
        ctx = self.ctx(session)
        for signal in signals:
            if signal.type in GENERIC_SIGNAL_TYPES:
                await self.handle_generic_signal(session, instance, signal, source_event_id=source_event_id)
                await definition.on_generic_outcome(instance, signal, ctx)  # workflow-side effects only
            else:
                await definition.handle_domain_signal(instance, signal, ctx)
        instance.updated_at = self.clock.now()
        await session.flush()

    async def handle_generic_signal(
        self,
        session: AsyncSession,
        instance: WorkflowInstance,
        signal: Signal,
        *,
        source_event_id: uuid.UUID | None = None,
    ) -> None:
        ctx = self.ctx(session)
        now = self.clock.now()
        definition = self.definition_for(instance)

        if isinstance(signal, Reschedule):
            contact = await self._target_contact(session, instance)
            at = apply_scheduling_constraints(now + parse_duration(signal.wait_duration), contact, key=str(instance.id))
            instance.attempt_count = 0
            await self.scheduler.schedule_wake(session, instance, at, reason="dynamic_reschedule")
            await ctx.log(instance, "outcome_recorded", {"outcome": "reschedule", "wait": signal.wait_duration, "at": at.isoformat()})

        elif isinstance(signal, NoAnswer):
            delay = resolve_retry_delay(definition.retry_policy, instance.attempt_count)
            if delay is None:
                await ctx.log(instance, "outcome_recorded", {"outcome": "no_answer", "attempt": instance.attempt_count, "retry": "exhausted"})
                await self.scheduler.clear_wake(instance)  # handing off - stop the clock
                await create_review_task(
                    session,
                    instance,
                    "stalled_no_response",
                    now=now,
                    suggested_options=["retry", "try alternate channel", "close"],
                    extra={"attempts": instance.attempt_count},
                )
            else:
                contact = await self._target_contact(session, instance)
                at = apply_scheduling_constraints(now + delay, contact, key=str(instance.id))
                instance.attempt_count += 1
                await self.scheduler.schedule_wake(session, instance, at, reason="retry")
                await ctx.log(
                    instance,
                    "outcome_recorded",
                    {"outcome": "no_answer", "attempt": instance.attempt_count, "retry_in": str(delay), "at": at.isoformat()},
                )

        elif isinstance(signal, EntityUpdate):
            fact = await self.write_back.apply(session, signal, source_event_id=source_event_id, now=now)
            await ctx.log(
                instance,
                f"fact_{fact.status}",  # fact_applied | fact_proposed | fact_rejected
                {
                    "entity_type": fact.entity_type,
                    "entity_id": str(fact.entity_id),
                    "field": fact.field,
                    "old_value": fact.old_value,
                    "new_value": fact.new_value,
                    "confidence": fact.confidence,
                    "fact_id": str(fact.id),
                },
            )
            if fact.status == "proposed":
                await create_review_task(
                    session,
                    instance,
                    "low_confidence_fact",
                    now=now,
                    suggested_options=["apply", "reject"],
                    extra={"fact_id": str(fact.id), "field": fact.field, "new_value": fact.new_value},
                )
            elif fact.status == "applied":
                await self._retry_after_correction(session, instance, fact, now)

        elif isinstance(signal, ActionRequired):
            # The other party is waiting on US. Park the requirement on the instance so
            # it survives restarts and shows up in the review queue with its details,
            # then stop chasing until a human records it as done.
            requirement = {
                "action_type": signal.action_type,
                "summary": signal.summary,
                "details": signal.details,
                "evidence": signal.evidence,
                "requested_at": now.isoformat(),
            }
            instance.context = {**instance.context, "pending_requirement": requirement}
            await ctx.log(instance, "action_required", requirement)
            if signal.blocks_progress:
                await self.scheduler.clear_wake(instance)
                await create_review_task(
                    session, instance, f"action_required:{signal.action_type}", now=now,
                    suggested_options=signal.suggested_options or ["mark completed", "close"],
                    extra={"requirement": requirement},
                )

        elif isinstance(signal, NeedsHuman):
            await self.scheduler.clear_wake(instance)  # handing off - stop the clock
            await create_review_task(session, instance, signal.reason, now=now, suggested_options=signal.suggested_options)

        else:  # pragma: no cover - GENERIC_SIGNAL_TYPES and these branches must stay in sync
            raise ValueError(f"generic signal without handler: {signal.type}")

    async def _apply_resolution_channel(
        self, session: AsyncSession, instance: WorkflowInstance, resolution: dict[str, Any],
        source_event_id: uuid.UUID | None, now: datetime,
    ) -> None:
        """"Try them another way" as a resolution: the human's channel choice is stored
        as a preferred_channel fact on the target contact, so every later send follows
        it. Same write-back path the brain uses - a person is just a very confident
        source."""
        channel = resolution.get("channel")
        target = instance.context.get("target_contact_id")
        if not channel or not target or channel not in CHANNEL_ADDRESS_FIELD:
            return
        fact = await self.write_back.apply(
            session,
            EntityUpdate(entity_type="contact", entity_id=str(target), field="preferred_channel",
                         new_value=channel, confidence=1.0, evidence="chosen by a reviewer"),
            source_event_id=source_event_id, now=now,
        )
        await log_event(
            session, instance.id, f"fact_{fact.status}",
            {"entity_type": fact.entity_type, "entity_id": str(fact.entity_id), "field": fact.field,
             "old_value": fact.old_value, "new_value": fact.new_value, "confidence": fact.confidence,
             "fact_id": str(fact.id), "source": "review"},
            now=now,
        )

    async def _complete_pending_requirement(
        self, session: AsyncSession, instance: WorkflowInstance, resolution: dict[str, Any], by: str, now: datetime
    ) -> bool:
        """"We paid the fee / submitted the portal form" -> resume outreach, keeping the
        reference on the instance so the next message can cite it. Generic: the Engine
        never needs to know what kind of requirement it was."""
        pending = instance.context.get("pending_requirement")
        if not pending or resolution.get("action") not in REQUIREMENT_DONE_ACTIONS:
            return False
        done = {**pending, "completed_at": now.isoformat(), "completed_by": by, "resolution": resolution}
        context = {k: v for k, v in instance.context.items() if k != "pending_requirement"}
        context["completed_requirements"] = [*instance.context.get("completed_requirements", []), done]
        instance.context = context
        instance.attempt_count = 0
        await log_event(session, instance.id, "requirement_completed", done, now=now)
        await self.scheduler.schedule_wake(session, instance, now, reason="resume_after_requirement")
        return True

    async def _retry_after_correction(
        self, session: AsyncSession, instance: WorkflowInstance, fact, now: datetime
    ) -> None:
        """Being told the right number is a reason to try again now, not in three days.

        The attempt that is being waited on went somewhere that does not reach them, so
        the deadline armed for it is measuring silence from a wrong address. Only fires
        while that deadline is what the instance is waiting on: a reply that also asked
        us to call back in a fortnight sets `dynamic_reschedule`, and that answer wins.
        """
        if fact.field not in REACHABILITY_FIELDS or instance.status != "active":
            return
        if str(fact.entity_id) != str(instance.context.get("target_contact_id") or ""):
            return  # they corrected somebody else's details, not the one we are chasing
        if instance.wake_reason not in (None, "response_timeout"):
            return

        contact = await self._target_contact(session, instance)
        at = apply_scheduling_constraints(now + RETRY_AFTER_CORRECTION, contact, key=str(instance.id))
        await self.scheduler.schedule_wake(session, instance, at, reason="retry_after_correction")
        await self.ctx(session).log(
            instance, "outcome_recorded",
            {"outcome": "reachability_corrected", "field": fact.field, "at": at.isoformat()},
        )

    # -- human-in-the-loop (minimal resolve path) ----------------------------------
    async def resolve_review_task(
        self, session: AsyncSession, task_id: uuid.UUID, resolution: dict[str, Any], resolved_by: str
    ) -> ReviewTask:
        task = await session.get(ReviewTask, task_id)
        if task is None:
            raise KeyError(f"review task not found: {task_id}")
        if task.status != "pending":
            return task
        now = self.clock.now()
        instance = await self._lock_instance(session, task.instance_id)
        task.status = "resolved"
        task.resolution = resolution
        task.resolved_by = resolved_by
        task.resolved_at = now
        await session.flush()
        if instance is None:
            return task
        pending = await session.scalar(
            select(func.count()).select_from(ReviewTask).where(ReviewTask.instance_id == instance.id, ReviewTask.status == "pending")
        )
        was_blocked = instance.status == "blocked"
        token_before = instance.wake_token
        if was_blocked and not pending:
            instance.status = "active"
        instance.updated_at = now
        review_event = await log_event(
            session, instance.id, "review_resolved",
            {"review_task_id": str(task.id), "reason": task.reason, "resolution": resolution, "by": resolved_by}, now=now,
        )
        await self._apply_resolution_channel(session, instance, resolution, review_event.id, now)
        await self._resolve_proposed_fact(session, instance, task, resolution, now)
        await self._complete_pending_requirement(session, instance, resolution, resolved_by, now)
        await self.definition_for(instance).on_review_resolved(instance, task, self.ctx(session))
        await self._rearm_wake_if_unblocked(session, instance, was_blocked, token_before, now)
        await session.flush()
        return task

    async def _resolve_proposed_fact(
        self, session: AsyncSession, instance: WorkflowInstance, task: ReviewTask,
        resolution: dict[str, Any], now: datetime,
    ) -> None:
        """A person's verdict on a fact the extraction was not sure enough to apply.

        `apply` is worth exactly what an auto-applied fact is worth: it wins on read
        from here on, and if it changed how to reach someone it re-arms the chase at
        once rather than in three days. `reject` closes the row off so it can never
        win, while leaving it visible - a wrong extraction is evidence too.

        Newest-applied still wins on read, so a better correction that landed while
        this one sat in the queue keeps its precedence. That is the honest ordering:
        approving an older guess does not un-learn a newer fact.
        """
        action = resolution.get("action")
        fact_id = (task.context_snapshot or {}).get("fact_id")
        if action not in ("apply", "reject") or not fact_id:
            return
        fact = await session.get(EntityFactVersion, uuid.UUID(str(fact_id)))
        if fact is None or fact.status != "proposed":
            return  # already decided, or the snapshot points at nothing

        fact.status = "applied" if action == "apply" else "rejected"
        await session.flush()
        await log_event(
            session, instance.id, f"fact_{fact.status}",
            {"entity_type": fact.entity_type, "entity_id": str(fact.entity_id), "field": fact.field,
             "old_value": fact.old_value, "new_value": fact.new_value, "confidence": fact.confidence,
             "fact_id": str(fact.id), "source": "review"},
            now=now,
        )
        if fact.status == "applied":
            await self._retry_after_correction(session, instance, fact, now)

    async def record_outcome(
        self, session: AsyncSession, instance_id: uuid.UUID, action: str, resolved_by: str
    ) -> WorkflowInstance:
        """A person recording what actually happened, without waiting to be asked.

        A workflow's `resolution_options` are endings it already knows how to carry
        out, and they were only reachable from a review task - so a chase that never
        escalated could not be closed off at all. Someone who learns the records
        arrived by other means (they turned up in the post, a colleague was told on
        another line) had nowhere to say so, and the only always-present button was
        the generic close, which records nothing about why it ended.

        Same handler and the same audit trail as a review resolution; the difference
        is only who started the conversation. The decision is written down as a
        resolved review task because that is where this platform keeps the record of
        a human deciding something.
        """
        instance = await self._lock_instance(session, instance_id)
        if instance is None:
            raise KeyError(f"instance not found: {instance_id}")
        definition = self.definition_for(instance)
        allowed = {o["action"] for o in definition.outcomes_for(instance)}
        if action not in allowed:
            raise ValueError(
                f"{instance.workflow_type} has no outcome {action!r}"
                + (f" - it records {', '.join(sorted(allowed))}" if allowed else "")
            )
        if instance.status == "completed":
            return instance

        now = self.clock.now()
        task = ReviewTask(
            id=uuid.uuid4(), instance_id=instance.id, reason="outcome_recorded",
            context_snapshot={"state": instance.state, "attempt_count": instance.attempt_count},
            suggested_options=[], status="resolved", resolution={"action": action},
            resolved_by=resolved_by, created_at=now, resolved_at=now,
        )
        session.add(task)
        await session.flush()
        await log_event(
            session, instance.id, "outcome_recorded",
            {"outcome": action, "by": resolved_by, "source": "staff"}, now=now,
        )
        instance.updated_at = now
        await definition.on_review_resolved(instance, task, self.ctx(session))
        await session.flush()
        return instance

    async def _rearm_wake_if_unblocked(
        self, session: AsyncSession, instance: WorkflowInstance, was_blocked: bool,
        token_before: str | None, now: datetime,
    ) -> None:
        """A wake scheduled while the instance was blocked has a durable job that fires
        into `skipped:status=blocked` and is then gone. Once the last review task
        clears, re-issue the job so recovery does not depend on the poller.

        Skipped when this resolution already scheduled one - the fencing token changing
        is the tell - so resolving does not write two identical wake_scheduled events.
        """
        if not was_blocked or instance.status != "active" or instance.next_wake_at is None:
            return
        if instance.wake_token != token_before:
            return  # something in this resolution already armed a fresh wake
        at = max(instance.next_wake_at, now) if instance.next_wake_at < now else instance.next_wake_at
        await self.scheduler.schedule_wake(session, instance, at, reason=instance.wake_reason or "resume_after_review")
