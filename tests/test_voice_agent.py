"""The call agent decides the next sentence while someone is on the line.

Failure here is dead air, so the degradation path is the point: Gemini key 1, key 2,
the local model, then a hand-off line. Every branch is covered without a network.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.voice.agent import HANDOFF_LINE, CallTurn, LlmCallAgent, ScriptedCallAgent, render_transcript

GOAL = "Find out whether the medical records for Jane Okafor have been sent."
OPENING = "Hi, I'm calling about a records request for Jane Okafor."
TRANSCRIPT = [
    {"who": "agent", "text": OPENING},
    {"who": "contact", "text": "We can't release those without an authorization."},
]


def gemini_turn(say: str, done: bool = False, reason: str | None = None) -> httpx.Response:
    payload = {"say": say, "done": done}
    if reason:
        payload["reason"] = reason
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def agent(responder, **kw) -> LlmCallAgent:
    return LlmCallAgent(
        kw.pop("keys", ["k1", "k2"]),
        client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        local_model=kw.pop("local_model", None),
        **kw,
    )


# ---------------------------------------------------------------- scripted double
async def test_the_scripted_agent_says_its_lines_then_ends():
    scripted = ScriptedCallAgent(["Could you send them over?", "Thank you."])
    assert (await scripted.next_turn(GOAL, OPENING, TRANSCRIPT)).say == "Could you send them over?"
    assert (await scripted.next_turn(GOAL, OPENING, TRANSCRIPT)).say == "Thank you."
    last = await scripted.next_turn(GOAL, OPENING, TRANSCRIPT)
    assert last.done is True


async def test_the_scripted_agent_records_what_it_was_shown():
    """So a session test can assert the agent actually saw the caller's words."""
    scripted = ScriptedCallAgent(["ok"])
    await scripted.next_turn(GOAL, OPENING, TRANSCRIPT)
    assert scripted.seen[0][-1]["text"] == "We can't release those without an authorization."


# ---------------------------------------------------------------- the real agent
async def test_it_asks_for_one_spoken_sentence_and_parses_the_answer():
    seen: dict = {}

    def responder(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return gemini_turn("Who should the authorization be addressed to?", done=False)

    turn = await agent(responder).next_turn(GOAL, OPENING, TRANSCRIPT)
    assert turn.say == "Who should the authorization be addressed to?" and turn.done is False
    assert turn.source == "gemini"

    system = seen["body"]["systemInstruction"]["parts"][0]["text"]
    assert GOAL in system and OPENING in system
    assert "short spoken sentences" in system, "it is talking, not writing"
    assert "We can't release those" in seen["body"]["contents"][0]["parts"][0]["text"], "it can hear them"
    assert seen["body"]["generationConfig"]["responseSchema"]["required"] == ["say", "done"]


async def test_it_can_end_the_call():
    turn = await agent(lambda r: gemini_turn("Thanks, I'll get that sent over.", done=True, reason="goal met")).next_turn(
        GOAL, OPENING, TRANSCRIPT
    )
    assert turn.done is True and turn.reason == "goal met"


async def test_a_spent_key_rotates_mid_call():
    """The failure we actually hit. Rotation must not cost a pause on a live line."""
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        seen.append(key)
        if key == "k1":
            return httpx.Response(429, json={"error": "quota"})
        return gemini_turn("Could you email it instead?")

    turn = await agent(responder).next_turn(GOAL, OPENING, TRANSCRIPT)
    assert seen == ["k1", "k2"] and turn.say == "Could you email it instead?"


async def test_both_keys_spent_falls_back_to_the_local_model(monkeypatch):
    def all_spent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "quota"})

    calls: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"response": '"Let me take a note of that."'}).encode()

    def fake_urlopen(request, timeout=None):
        calls.append(json.loads(request.data))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    turn = await agent(all_spent, local_model="llama3.2:3b").next_turn(GOAL, OPENING, TRANSCRIPT)
    assert turn.source == "local"
    assert turn.say == "Let me take a note of that.", "quotes the small model adds are stripped"
    assert turn.done is False, "a local turn never decides to hang up"
    assert calls and calls[0]["model"] == "llama3.2:3b"


async def test_everything_failing_hands_off_rather_than_leaving_dead_air(monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    def no_ollama(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", no_ollama)

    turn = await agent(down, local_model="llama3.2:3b").next_turn(GOAL, OPENING, TRANSCRIPT)
    assert turn.say == HANDOFF_LINE and turn.done is True and turn.source == "handoff"


async def test_an_empty_answer_is_treated_as_a_failure():
    """A model that returns "" would otherwise produce silence on the line."""
    turn = await agent(lambda r: gemini_turn("   ")).next_turn(GOAL, OPENING, TRANSCRIPT)
    assert turn.say == HANDOFF_LINE and turn.done is True


# ---------------------------------------------------------------- transcript shape
def test_the_transcript_reads_as_a_conversation():
    assert render_transcript(TRANSCRIPT) == (
        "You: Hi, I'm calling about a records request for Jane Okafor.\n"
        "Them: We can't release those without an authorization."
    )


@pytest.mark.live
async def test_live_agent_answers_a_records_desk():
    import os

    from app.config import get_settings

    settings = get_settings("postgresql://x/y")
    if not settings.gemini_api_keys:
        pytest.skip("no GEMINI_API_KEY")
    live = LlmCallAgent(settings.gemini_api_keys, model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"))
    turn = await live.next_turn(GOAL, OPENING, TRANSCRIPT)
    assert turn.say and len(turn.say) < 300, f"got {turn.say!r}"
    assert turn.source == "gemini"


def test_the_agent_is_told_it_is_reading_a_transcription():
    """On a live call it never sees words, only a machine's guess at them - so it must
    verify anything it was sent to collect rather than repeat a mishearing back."""
    from app.voice.agent import SYSTEM

    system = SYSTEM.format(goal=GOAL, opening=OPENING)
    assert "speech recognition's guess" in system
    assert "spell it out" in system, "for anything that has to be exact"
    assert "did not catch it rather than guessing" in system
    assert "wrong facts on a client's file" in system, "the reason it matters"
