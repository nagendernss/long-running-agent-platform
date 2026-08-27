# Voice Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mocked `call` channel with a real, two-way voice conversation that a person answers in a browser, transcribed locally, ending in a transcript the existing brain turns into signals.

**Architecture:** A call is still *one attempt*. `ctx.send(channel="call")` places it and returns; the conversation runs outside the engine transaction; the transcript is submitted through `handle_inbound`. The engine does not change.

**Tech Stack:** faster-whisper (`small.en`, already cached), ffmpeg, browser `speechSynthesis` + Web Audio, FastAPI WebSockets, Gemini with key rotation, Ollama `llama3.2:3b` as last resort.

**Spec:** `docs/superpowers/specs/2026-08-27-voice-calls-design.md`

## Global Constraints

- `app/engine.py` and `app/scheduling/` must not change. `tests/test_extensibility.py` still asserts the engine names no workflow.
- Everything except the LLM runs locally. No new cloud dependency.
- The default test suite stays offline and microphone-free: STT and the agent are injected. Only `-m live` tests touch the model or the network.
- One call at a time in v1; a second `ringing` call for the same instance is refused.
- A call that is never answered must reach the existing retry ladder, not a new code path.

---

### Task 1: Local speech-to-text

**Files:**
- Create: `app/voice/__init__.py`, `app/voice/stt.py`
- Test: `tests/test_voice_stt.py`

**Interfaces:**
- Produces: `SpeechToText` protocol with `async transcribe(audio: bytes, mime: str) -> str`;
  `WhisperSTT(model_size="small.en")`; `ScriptedSTT(list[str])` for tests.

- [ ] **Step 1: Write the failing test**

```python
from app.voice.stt import ScriptedSTT

async def test_scripted_stt_returns_utterances_in_order():
    stt = ScriptedSTT(["hello", "we need an authorization"])
    assert await stt.transcribe(b"", "audio/webm") == "hello"
    assert await stt.transcribe(b"", "audio/webm") == "we need an authorization"
    assert await stt.transcribe(b"", "audio/webm") == ""   # nothing left = silence
```

- [ ] **Step 2: Run it, expect ModuleNotFoundError** — `.venv/Scripts/python -m pytest tests/test_voice_stt.py -q`

- [ ] **Step 3: Implement**

`WhisperSTT` loads the model once (lazily, on first use) and runs `transcribe` through `asyncio.to_thread` so the event loop is never blocked. Non-wav input is converted with ffmpeg via `subprocess.run` in the same thread. `beam_size=1` for latency. Empty or whitespace-only results return `""`, which the session treats as silence.

- [ ] **Step 4: Run tests, expect pass**

- [ ] **Step 5: Add the live test**

```python
@pytest.mark.live
async def test_whisper_transcribes_real_speech(tmp_path):
    """Proves the cached model works: SAPI speaks, whisper reads it back."""
    wav = tmp_path / "s.wav"
    subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                    f'Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;'
                    f' $s.SetOutputToWaveFile("{wav}"); $s.Speak("The records were faxed this morning."); $s.Dispose()'],
                   check=True, timeout=120)
    text = await WhisperSTT().transcribe(wav.read_bytes(), "audio/wav")
    assert "faxed" in text.lower()
```

- [ ] **Step 6: Commit** — `feat: local speech-to-text with faster-whisper`

---

### Task 2: The call agent

**Files:**
- Create: `app/voice/agent.py`
- Test: `tests/test_voice_agent.py`

**Interfaces:**
- Produces: `CallTurn(say: str, done: bool, reason: str | None)`; `CallAgent` protocol with `async next_turn(goal, opening, transcript) -> CallTurn`; `LlmCallAgent(gemini_keys, model, local_model=None, client=None)`; `ScriptedCallAgent(list[str])`.

- [ ] **Step 1: Write the failing tests** — a scripted agent returns its lines then `done`; the LLM agent parses `{say, done}` from structured output; a 429 on key 1 rotates to key 2; both keys failing falls to the local model; everything failing yields the hand-off line with `done=True`.

- [ ] **Step 2: Run them, expect failure**

- [ ] **Step 3: Implement** — the prompt carries the goal, the opening line, and the transcript so far, and asks for one short spoken sentence plus whether the call is finished. Reuse the key-rotation logic already in `app/llm_brain.py` (extract the shared bit rather than copying it). The local fallback posts to `http://127.0.0.1:11434/api/generate` with a 10s timeout. The final fallback is a fixed sentence: *"I'm sorry, I'm having trouble on this line - I'll have someone call you back."* with `done=True`.

- [ ] **Step 4: Run tests, expect pass**

- [ ] **Step 5: Commit** — `feat: goal-directed call agent with a degradation path`

---

### Task 3: The call record

**Files:**
- Create: `migrations/003_call.sql`
- Modify: `app/db/models.py`
- Create: `app/voice/repository.py`
- Test: `tests/test_call_repository.py`

**Interfaces:**
- Produces: `CallRow` model; `async place_call(...) -> CallRow`, `async get_call(...)`, `async append_turn(session, call_id, who, text, now)`, `async finish_call(session, call_id, status, now)`, `async ringing_call_for(session, instance_id)`.

- [ ] **Step 1: Write the failing test** — place a call, append two turns, finish it; assert status transitions and that the transcript preserves order.
- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Write the migration and repository** exactly as the spec's DDL.
- [ ] **Step 4: Run tests, expect pass**
- [ ] **Step 5: Commit** — `feat: calls are rows with a transcript`

---

### Task 4: The voice channel places calls

**Files:**
- Create: `app/channels/voice.py`
- Modify: `app/channels/__init__.py`, `app/runtime.py`
- Test: `tests/test_voice_channel.py`

**Interfaces:**
- Produces: `VoiceChannel(session_factory, clock, ring_timeout=45)`; implements `Channel.send`.

- [ ] **Step 1: Write the failing test**

```python
async def test_sending_on_the_call_channel_places_a_call_and_returns(rt, seed):
    """It must return immediately: a call lasts minutes, an engine transaction must not."""
    iid = await start_medical(rt, seed)          # provider_channel is "call"
    call = await latest_call(rt, iid)
    assert call.status == "ringing"
    assert call.opening.startswith("Hi Mercy Hospital Records")
    assert "records" in call.goal.lower()
    inst = await load(rt, iid)
    assert inst.wake_reason == "response_timeout", "the attempt finished; the call runs on its own"
```

- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Implement** — `send()` writes a `ringing` call row plus a `call_placed` event and returns its id. Non-`call` channels are delegated to a wrapped channel, so SMS and email keep working. The goal is assembled from the workflow's description and its `domain_signals` docstrings.
- [ ] **Step 4: Run tests, expect pass**
- [ ] **Step 5: Commit** — `feat: the call channel places real calls`

---

### Task 5: The session and the WebSocket protocol

**Files:**
- Create: `app/voice/session.py`
- Modify: `app/api/main.py`
- Test: `tests/test_voice_session.py`

**Interfaces:**
- Produces: `VoiceCallSession(call_id, stt, agent, session_factory, clock, engine)` with `async on_answer() -> str` (the opening line), `async on_utterance(audio, mime) -> CallTurn`, `async on_hangup(reason)`.
- WebSocket `/ws/call/{call_id}`: client sends `{"type":"answer"}`, binary audio frames, `{"type":"hangup"}`; server sends `{"type":"speak","text":...,"done":bool}` and `{"type":"transcript","who":...,"text":...}`.

- [ ] **Step 1: Write the failing test** — drive a whole call through the real protocol with `ScriptedSTT` and `ScriptedCallAgent`: answer, two utterances, hang up; assert the transcript is stored, `call_ended` is logged, and `handle_inbound` received the joined transcript and produced signals.
- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Implement** — `on_hangup` submits the transcript through `engine.handle_inbound(..., channel="call")` in its own transaction, so a call feeds the same extraction path as a typed reply. A dropped socket is a hangup with whatever transcript exists.
- [ ] **Step 4: Run tests, expect pass**
- [ ] **Step 5: Commit** — `feat: the call session turns speech into signals`

---

### Task 6: The phone page

**Files:**
- Create: `app/api/templates/phone.html`
- Modify: `app/api/main.py` (`/phone`), `app/api/templates/base.html` (nav)
- Test: `tests/test_voice_api.py`

- [ ] **Step 1: Write the failing test** — `/phone` renders and shows a ringing call; `/api/calls` lists it; after the session completes, the page shows it as ended.
- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Build the page** — polls `/api/calls?status=ringing`, rings (an oscillator beep, no asset), Answer opens the WebSocket, `getUserMedia` + `MediaRecorder` capture, Web Audio RMS ends a turn after ~800ms of quiet with a hold-to-talk fallback, replies spoken with `speechSynthesis`, live transcript, Hang up. Respects `prefers-reduced-motion` and keyboard focus.
- [ ] **Step 4: Run the whole suite**
- [ ] **Step 5: Commit** — `feat: answer calls in the browser`

---

### Task 7: Calls in the instance timeline

**Files:**
- Modify: `app/api/timeline.py`, `app/api/templates/_timeline.html`
- Test: `tests/test_voice_api.py` (add)

- [ ] **Step 1: Write the failing test** — an instance that had a call shows the conversation in its timeline, agent and contact lines on their own lanes.
- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Implement** — `call_placed` opens a round labelled *Call*; each transcript turn is a node on the lane for who spoke; `call_ended` closes it with the outcome.
- [ ] **Step 4: Run the whole suite**
- [ ] **Step 5: Commit** — `feat: a call reads as a conversation in the timeline`

---

### Task 8: Wire it up and document

**Files:** Modify `app/runtime.py`, `scripts/serve.py`, `README.md`, `.env.example`

- [ ] **Step 1: Make the voice channel opt-in** — `VOICE_CALLS=1` (or `--voice` on serve) swaps `MockChannel` for `VoiceChannel` wrapping it, so the default suite and demo stay untouched.
- [ ] **Step 2: Document it** — what runs locally, the measured latency, how to answer a call, and that the compliance stub now matters because this dials for real.
- [ ] **Step 3: Run the whole suite, plus `-m live` once by hand**
- [ ] **Step 4: Commit** — `docs: voice calls`
