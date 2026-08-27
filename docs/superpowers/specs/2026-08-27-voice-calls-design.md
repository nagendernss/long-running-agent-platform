# Voice calls: a real conversation, not a mocked message

**Date:** 2026-08-27
**Status:** approved for planning

## Problem

Every outreach so far is one-way and text-shaped. `ctx.send(channel="call")` writes a
row and returns; a human types a reply into a form; the brain reads it. The `call`
channel is a label, not a call.

A real call is different in kind, not degree: it is synchronous, multi-turn, and has
to answer within a couple of seconds or the other person starts talking again. The
agent must hold a goal ("get the records for Jane Okafor"), react to what it hears,
and come away with the facts a paralegal needs.

## Goals

1. Place a real call that rings in a browser window, which a person answers and speaks to.
2. Transcribe their speech locally and speak back, holding a goal-directed conversation.
3. End with a transcript that flows into the existing extraction path, producing the
   same signals as a typed reply.
4. Change nothing in the engine.

## Non-goals

- Real telephony. The "phone" is a browser page; a PSTN provider is a channel swap later.
- Barge-in (talking over the agent), and multiple simultaneous calls.
- Streaming partial transcripts. Turns are whole utterances.
- Voice cloning or a TTS model. The browser's `speechSynthesis` uses the Windows
  voices already installed.

## What is on the machine (measured, not assumed)

| Piece | What | Evidence |
|---|---|---|
| STT | `faster-whisper-small.en`, 925 MB, already in the HF cache | loads in 15.6s; transcribed SAPI speech verbatim in 1.8s |
| TTS | Windows SAPI voices (David, Zira) via the browser | no model needed, no server audio path |
| LLM (fallback) | Ollama `llama3.2:3b` | 0.4s per turn warm, 6.9s cold |
| Audio | ffmpeg 8.1 | webm to wav |

Turn budget: **~2s STT + 1-3s Gemini = 3-5s per turn.** Noticeable, workable, and
honest about what a CPU can do.

## Design

### The invariant: a call is one attempt

The engine is not taught about calls. `ctx.send(channel="call")` places one and
returns immediately - it must, because a call lasts minutes and an engine transaction
must not. The conversation happens outside that transaction, and its transcript is
submitted through `handle_inbound` exactly as a typed reply is.

```
wake -> on_wake -> ctx.send(channel="call") -> call row (ringing) -> returns, transaction commits
                                                    |
                     [browser rings; a person answers and talks]
                                                    |
                                                    v
                       transcript -> handle_inbound -> brain -> signals -> engine
```

Everything already built therefore keeps working unchanged: the retry ladder, the
response deadline, fact write-back, requirements, the review queue. Nobody answers ->
the call is `missed` -> submitted as a no-answer -> the ladder fires. That is the
existing machinery paying for itself again.

### Components

| Piece | File | Job |
|---|---|---|
| Phone page | `app/api/templates/phone.html` | rings, answer/decline, mic capture, speaks replies, live transcript, hang up |
| WebSocket | `app/api/main.py` (`/ws/call/{id}`) | audio chunks up; `{speak, done}` down |
| Session | `app/voice/session.py` | the turn loop and call state |
| STT | `app/voice/stt.py` | faster-whisper, loaded once, run off the event loop |
| Call agent | `app/voice/agent.py` | goal + transcript -> next utterance, or hang up |
| Channel | `app/channels/voice.py` | implements `Channel`; places a call instead of sending text |

Turn detection is **browser-side**: Web Audio RMS, ~800ms of quiet ends the caller's
turn, with a hold-to-talk fallback. No server VAD, no half-duplex guessing.

### The turn loop

```
caller speaks -> browser detects silence -> sends the utterance's audio
  -> ffmpeg webm->wav -> faster-whisper -> text
  -> call agent (goal + transcript so far) -> {say, done}
  -> browser speaks it with speechSynthesis
  -> repeat until done, hang up, or the socket drops
```

### Degradation, in order

The call agent's failure path was chosen because we hit 429s repeatedly during
development, and dead air mid-call is worse than a blunt sentence:

```
Gemini key 1  ->  Gemini key 2  ->  llama3.2:3b (local, 0.4s)  ->  "let me have someone call you back"
```

Key rotation already exists in the brain. The local model is the last resort because a
network outage kills both keys equally.

### Data

```sql
CREATE TABLE call (
    id          UUID PRIMARY KEY,
    instance_id UUID REFERENCES workflow_instance(id),
    contact_id  UUID REFERENCES contact(id),
    status      TEXT NOT NULL,           -- ringing | active | completed | missed | failed
    goal        TEXT,
    opening     TEXT,                    -- the first thing the agent says
    transcript  JSONB NOT NULL DEFAULT '[]',   -- [{who: agent|contact, text, at}]
    created_at, answered_at, ended_at
);
CREATE INDEX idx_call_ringing ON call (created_at) WHERE status = 'ringing';
```

Events: `call_placed`, `call_answered`, `call_ended`, `call_missed`. The instance
timeline renders a call as its own round with the transcript inline, so a call reads
like the conversation it was.

### What the agent is told

Nothing new to configure. The opening line is the message the workflow would have
sent. The goal is built from the workflow's description plus its `domain_signals`
docstrings - "find out whether the records were sent; if they need an authorisation or
a fee, get the particulars" is already written down in `RecordsReceived`,
`AuthRequired` and `RequestDenied`.

### Failure modes, deliberately

| What happens | Result |
|---|---|
| Nobody answers within 45s | `missed`, submitted as a no-answer, retry ladder fires |
| Every LLM path fails | agent says it will have someone call back, ends the call |
| Whisper returns noise | the agent asks them to repeat rather than acting on it |
| Browser closed mid-call | socket drops, call `completed` with the partial transcript, still submitted |
| Two calls at once | the second is refused; v1 is one call at a time |

## Testing

`VoiceCallSession` takes its STT and agent as injected dependencies, so the suite
drives an entire call over the real WebSocket protocol with a fake STT (text in) and a
scripted agent - no microphone, no model, no network. Covered: a full answered call
producing signals, a missed call feeding the ladder, a dropped socket, agent failure
falling through to the local model, and the transcript reaching `handle_inbound`.

One `live` test does the real round trip - SAPI generates audio, real whisper
transcribes it - mirroring the probe that proved the stack.

## Risks accepted

- 3-5s per turn on CPU. `tiny.en` would halve the STT half if it becomes annoying.
- One call at a time, in-process. Concurrent calls need a session registry that
  survives more than one worker.
- The browser is the phone. Everything about audio capture is Chrome-flavoured.
- No compliance checks on placing a call; `apply_compliance_window()` is still a stub
  and now matters more, because this dials for real.

## Shape of the change

| Area | Change |
|---|---|
| `app/voice/{stt,agent,session}.py` | new |
| `app/channels/voice.py` | new - places calls |
| `migrations/003_call.sql` | new - the `call` table |
| `app/api/main.py` | `/phone`, `/ws/call/{id}`, call JSON endpoints |
| `app/api/templates/phone.html` | new |
| `app/api/timeline.py` | render a call round with its transcript |
| `app/engine.py` | none |
