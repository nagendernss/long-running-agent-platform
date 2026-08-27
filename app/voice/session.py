"""One call, from ring to transcript.

The session owns the turn loop and nothing else. It does not know about WebSockets -
the API layer feeds it utterances and speaks whatever it returns - so the whole thing
is testable with a fake ear and a scripted mouth.

Where it hands back to the platform matters: `on_hangup` submits what the *other
party* said through `Engine.handle_inbound`, in its own transaction. From that point
a call is indistinguishable from a typed reply, which is why nothing in the engine
needed to change to support voice.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.clock import Clock
from app.events import log_event
from app.voice.agent import CallAgent, CallTurn
from app.voice.repository import (
    answer_call,
    append_turn,
    finish_call,
    get_call,
    transcript_text,
)
from app.voice.stt import SpeechToText

log = logging.getLogger(__name__)

# What we say when we heard nothing intelligible. Better than acting on noise.
DID_NOT_CATCH = "Sorry, I didn't catch that - could you say it again?"
MAX_UNINTELLIGIBLE = 3  # after this many, hand off rather than loop forever


@dataclass
class VoiceCallSession:
    call_id: uuid.UUID
    stt: SpeechToText
    agent: CallAgent
    session_factory: object
    clock: Clock
    engine: object
    transcript: list[dict] = field(default_factory=list)
    goal: str = ""
    opening: str = ""
    finished: bool = False
    _unintelligible: int = 0

    # -- lifecycle ------------------------------------------------------------------
    async def on_answer(self) -> str:
        """Somebody picked up. Returns the opening line to speak."""
        async with self.session_factory() as session, session.begin():
            call = await answer_call(session, self.call_id, self.clock.now())
            if call is None:
                raise LookupError(f"call {self.call_id} is not ringing")
            self.goal, self.opening = call.goal or "", call.opening or ""
            self.transcript = list(call.transcript or [])
            await append_turn(session, self.call_id, "agent", self.opening, self.clock.now())
            if call.instance_id:
                await log_event(session, call.instance_id, "call_answered",
                                {"call_id": str(self.call_id)}, now=self.clock.now())
        self.transcript.append({"who": "agent", "text": self.opening})
        return self.opening

    async def on_utterance(self, audio: bytes, mime: str = "audio/webm") -> CallTurn:
        """They said something. Returns what to say back, with the timings that
        produced it - a slow turn or a bad transcription should be visible."""
        started = time.perf_counter()
        heard = (await self.stt.transcribe(audio, mime)).strip()
        stt_ms = int((time.perf_counter() - started) * 1000)
        if not heard:
            self._unintelligible += 1
            if self._unintelligible >= MAX_UNINTELLIGIBLE:
                return CallTurn(
                    say="I'm having trouble hearing you - I'll have someone call you back.",
                    done=True, reason="could not hear", source="handoff", stt_ms=stt_ms,
                )
            # Deliberately not sent to the agent: it would have to invent a reply to
            # silence, and inventing is exactly what we do not want on a call.
            return CallTurn(say=DID_NOT_CATCH, done=False, source="handoff", stt_ms=stt_ms)

        self._unintelligible = 0
        await self._record("contact", heard)

        thinking = time.perf_counter()
        turn = await self.agent.next_turn(self.goal, self.opening, self.transcript)
        turn.heard, turn.stt_ms = heard, stt_ms
        turn.agent_ms = int((time.perf_counter() - thinking) * 1000)
        await self._record("agent", turn.say, source=turn.source)
        return turn

    async def on_hangup(self, reason: str = "hung up", status: str = "completed") -> None:
        """The call is over, however it ended. Hand the transcript to the platform."""
        if self.finished:
            return
        self.finished = True

        async with self.session_factory() as session, session.begin():
            call = await finish_call(session, self.call_id, status, self.clock.now())
            if call is None:
                return
            said = transcript_text(call)
            instance_id, turns = call.instance_id, len(call.transcript or [])
            if instance_id:
                await log_event(
                    session, instance_id,
                    "call_ended" if status == "completed" else f"call_{status}",
                    {"call_id": str(self.call_id), "reason": reason, "turns": turns,
                     "heard": said[:500]},
                    now=self.clock.now(),
                )

        if not instance_id:
            return
        # From here a call is just an inbound message. Empty (nobody spoke) is exactly
        # what the brain reads as a non-answer, which is what it was.
        async with self.session_factory() as session, session.begin():
            await self.engine.handle_inbound(session, instance_id, said, channel="call")

    # -- internals ------------------------------------------------------------------
    async def _record(self, who: str, text: str, source: str | None = None) -> None:
        now: datetime = self.clock.now()
        self.transcript.append({"who": who, "text": text, "at": now.isoformat()})
        async with self.session_factory() as session, session.begin():
            await append_turn(session, self.call_id, who, text, now, source=source)


async def miss_call(session_factory, clock: Clock, engine, call_id: uuid.UUID, reason: str = "not answered") -> None:
    """Nobody picked up. Deliberately routed through the same door as a real call, so
    an unanswered call reaches the existing retry ladder rather than a new code path."""
    async with session_factory() as session, session.begin():
        call = await get_call(session, call_id)
        if call is None or call.status != "ringing":
            return
        await finish_call(session, call_id, "missed", clock.now())
        instance_id = call.instance_id
        if instance_id:
            await log_event(session, instance_id, "call_missed",
                            {"call_id": str(call_id), "reason": reason}, now=clock.now())
    if instance_id:
        async with session_factory() as session, session.begin():
            await engine.handle_inbound(session, instance_id, "", channel="call")
