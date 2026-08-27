"""The call agent: what to say next, and when to stop.

This is not the Agent Brain. The brain reads a finished conversation and extracts
signals, with the whole message in front of it and no one waiting. This runs *during*
a call, with someone on the line, and its only job is the next sentence.

Failure here is different in kind too. A brain that fails falls back to keyword
matching and nobody notices for a while; an agent that fails leaves dead air. So it
degrades through everything available before it gives up:

    Gemini key 1  ->  Gemini key 2  ->  llama3.2:3b (local, ~0.4s)  ->  hand-off line

The local model is last because a network outage takes both keys with it, and a blunt
sentence from a small model is much better than silence.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Protocol, Sequence

from app.gemini import GeminiClient

log = logging.getLogger(__name__)

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
LOCAL_MODEL = "llama3.2:3b"
HANDOFF_LINE = "I'm sorry, I'm having trouble on this line - I'll have someone call you back."

SYSTEM = """You are making a phone call on behalf of a law firm. You are speaking out loud, so every reply must be one or two short spoken sentences - no lists, no formatting, no reading out URLs character by character.

Your goal for this call:
{goal}

You opened with: "{opening}"

What you are reading is not what they said - it is speech recognition's guess at it. Expect:
- numbers spelled out ("forty five dollars", "five five five oh one nine nine") and dates as words
- names, street names and companies mangled or invented outright
- missing words, run-on sentences, no punctuation, and homophones ("fax" for "facts", "no" for "know")
- an empty or nonsense line when someone coughs, a door slams, or two people talk at once

So:
- Never repeat a garbled name or address back as though it were correct. If a detail you were sent to collect - an amount, an address, a phone number, a date, a reference - is unclear, implausible, or half-heard, ask them to repeat it, and for anything spelled ask them to spell it out.
- Read spoken numbers as numbers: "forty five dollars" is $45, "the third" is a date.
- If a whole turn is unintelligible, say you did not catch it rather than guessing at it. Guessing puts wrong facts on a client's file.
- If what you heard contradicts something earlier in the call, ask rather than assume the newer one is right.

Rules:
- Ask for what you need plainly, then listen. Do not volunteer legal advice.
- If they give you something you were sent to find out - a form, a date, a different number, an amount - repeat it back so it is captured accurately, then move on.
- Never raise money yourself. Do not ask whether there is a fee, what it costs, or how to pay. If they bring it up, take the details down and repeat them back; if they do not, it does not come up.
- If they ask you to call back later, accept it and confirm roughly when.
- If they need to transfer you or take a message, accept that too.
- If they give you a different number, address or inbox to use, read it back to confirm it and say the firm will follow up there. Never say you cannot call, cannot email, or cannot follow up - the firm does exactly that, and this call is how it finds out where.
- End the call as soon as it has nothing left to achieve: the goal is met, they have told you what to do next, they have given you somewhere else to try, or they cannot help. Say thank you and stop. Repeating a question they have already answered is worse than ending a minute early.
- Never apologise more than once for the same thing, and never explain your own limitations twice.
- You are not a person. If asked directly, say you are an automated assistant for the firm - once, and then carry on with the call."""

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "done": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["say", "done"],
}


@dataclass
class CallTurn:
    say: str
    done: bool = False
    reason: str | None = None
    source: str = "gemini"  # gemini | local | handoff | scripted - useful in the timeline
    # What the machinery did to produce this, surfaced on the phone page so a bad
    # transcription or a slow turn is visible rather than mysterious.
    heard: str = ""
    stt_ms: int = 0
    agent_ms: int = 0


class CallAgent(Protocol):
    async def next_turn(self, goal: str, opening: str, transcript: Sequence[dict]) -> CallTurn: ...


class ScriptedCallAgent:
    """Test double: says its lines in order, then ends the call."""

    def __init__(self, lines: Sequence[str] | None = None, closing: str = "Thanks very much, goodbye."):
        self._lines = list(lines or [])
        self._closing = closing
        self.seen: list[list[dict]] = []

    async def next_turn(self, goal: str, opening: str, transcript: Sequence[dict]) -> CallTurn:
        self.seen.append([dict(t) for t in transcript])
        if self._lines:
            return CallTurn(say=self._lines.pop(0), done=False, source="scripted")
        return CallTurn(say=self._closing, done=True, reason="script finished", source="scripted")


def render_transcript(transcript: Sequence[dict]) -> str:
    who = {"agent": "You", "contact": "Them"}
    return "\n".join(f"{who.get(t.get('who'), t.get('who'))}: {t.get('text', '')}" for t in transcript)


class LlmCallAgent:
    def __init__(
        self,
        api_keys: str | Sequence[str],
        *,
        model: str | None = None,
        local_model: str | None = LOCAL_MODEL,
        timeout: float = 20.0,
        client=None,
    ):
        self.gemini = GeminiClient(api_keys, model=model or "gemini-3.6-flash", timeout=timeout, client=client)
        self.local_model = local_model
        self.timeout = timeout

    async def next_turn(self, goal: str, opening: str, transcript: Sequence[dict]) -> CallTurn:
        system = SYSTEM.format(goal=goal, opening=opening)
        user = "The call so far:\n" + render_transcript(transcript) + "\n\nWhat do you say next?"
        try:
            payload = await self.gemini.generate_json(system=system, user=user, schema=TURN_SCHEMA, temperature=0.3)
            say = (payload.get("say") or "").strip()
            if say:
                return CallTurn(say=say, done=bool(payload.get("done")), reason=payload.get("reason"))
            log.warning("gemini returned an empty turn; falling back")
        except Exception as exc:  # every key spent, a timeout, malformed JSON - all the same here
            log.warning("call agent: gemini failed (%s), falling back to the local model", type(exc).__name__)

        local = await self._local_turn(system, user)
        return local or CallTurn(say=HANDOFF_LINE, done=True, reason="no model available", source="handoff")

    async def _local_turn(self, system: str, user: str) -> CallTurn | None:
        """Last resort before dead air. Plain text, not JSON - a 3B model is much more
        reliable at one sentence than at a schema, and `done` is not worth the risk."""
        if not self.local_model:
            return None
        import asyncio

        def call() -> str:
            body = {
                "model": self.local_model,
                "prompt": f"{system}\n\n{user}\nReply with one short spoken sentence only.",
                "stream": False,
                "options": {"num_predict": 60, "temperature": 0.3},
            }
            request = urllib.request.Request(
                OLLAMA_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode()).get("response", "")

        try:
            text = (await asyncio.to_thread(call)).strip().strip('"')
        except Exception as exc:
            log.warning("call agent: local model unavailable (%s)", type(exc).__name__)
            return None
        return CallTurn(say=text, done=False, source="local") if text else None
