# HelloCounsel — Long-Running Agent Platform (working slice)

A platform for agent workflows that live for weeks: an agency starts an agent to
gather something from a third party (a provider, a clerk), the agent keeps chasing
on a schedule, reports every outcome back to the client, absorbs corrections to its
own data ("wrong number, use this one"), and escalates to a human when it is stuck
or when the client has to do something.

The point of this slice is the **engine**, not the use cases. Two reference
workflows ship (`medical_records_followup`, `client_checkin`), and a third is
defined inside a test to prove that adding one touches no engine code.

---

## Layers (the invariant)

```
inbound message / call transcript
        |
        v
1. Agent Brain          app/llm_brain.py         text -> [Signal]      (Gemini; rule baseline behind it)
        |
        v
2. Engine               app/engine.py            workflow-AGNOSTIC orchestrator
   |- generic signals   RESCHEDULE / NO_ANSWER / ENTITY_UPDATE /
   |                    ACTION_REQUIRED / NEEDS_HUMAN                          -> handled here
   |- domain signals    RECORDS_RECEIVED, CLIENT_FLAG, ...                     -> forwarded down
        |
        v
3. Workflow definition  app/workflows/*.py       declarative states + domain handlers
```

Supporting pieces the Engine owns, all generic:

| Concern | Module | Note |
|---|---|---|
| Durable scheduling | `app/scheduling/scheduler.py` | Procrastinate (Postgres) + fencing token |
| Business-hours / timezone clamp | `app/scheduling/constraints.py` | compliance window is a **stub** |
| Retry policy resolution | `app/retry.py` | policy is data on the workflow, not code |
| Versioned fact write-back | `app/write_back.py`, `app/db/facts.py` | gated by `config/field_registry.yaml` |
| Human-in-the-loop | `app/review.py`, `Engine._complete_pending_requirement` | `review_task`, resolve, and requirement hand-back |
| Channels | `app/channels/` | `MockChannel`; real telephony/email plugs in here |
| Agent Brain | `app/agent_brain.py`, `app/llm_brain.py` | keyword baseline + Gemini, chosen by config |
| Audit trail | `app/events.py` | append-only `event` table, monotonic `seq` |

`tests/test_extensibility.py` enforces the invariant twice: it runs a brand-new
workflow through the unmodified engine, and it greps `app/engine.py` and
`app/scheduling/scheduler.py` for any concrete workflow name.

---

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"    # Windows
# python -m venv .venv && .venv/bin/pip install -e ".[dev]"      # macOS/Linux

cp .env.example .env          # DATABASE_URL, and GEMINI_API_KEY for the LLM brain
docker compose up -d          # Postgres 16   (or skip: --embedded starts a bundled one)
```

### Agent Brain

Two interchangeable implementations behind one `AgentBrain` protocol:

| `AGENT_BRAIN` | Implementation | Notes |
|---|---|---|
| `rules` | `RuleBasedAgentBrain` | keyword matching, no network, deterministic — what the test suite runs on |
| `gemini` | `GeminiAgentBrain` | Google Gemini structured output, `gemini-3.6-flash` by default |

Setting `GEMINI_API_KEY` alone switches to the LLM; `AGENT_BRAIN=rules` forces the
baseline back on. `scripts/demo.py --brain gemini|rules` overrides per run.

The LLM brain is not a hardcoded prompt. Its JSON schema is generated from the
generic signal classes plus the workflow's own `domain_signals`, the writable-field
list comes from the Field Registry, and each signal's description comes from its
docstring — so a new workflow reshapes the prompt without anyone editing a prompt.

Three guard rails, because a model is not a trusted input:

- **It cannot redirect a write.** `ENTITY_UPDATE.entity_id` is overwritten with the
  contact the instance is actually talking to, and `entity_type` is forced to
  `contact`. The model chooses *what changed*, never *whose record changes*.
- **It cannot emit an unschedulable delay.** `Reschedule.wait_duration` is normalised
  by a validator on the signal model itself (`"two weeks"` → `2w`, `"14d …notes"` →
  `14d`, `"sometime soon"` → rejected). A live run really did return
  `"14dPool filter set / schedule follow up in 2 weeks"`; the scheduler never saw it.
- **It cannot lose a message.** Rate limits and 5xx are retried briefly (honouring
  `Retry-After`; a 401 is not retried), then any remaining error, timeout, malformed
  JSON or empty extraction falls back to the rule brain; if that finds nothing either,
  the Engine raises `NEEDS_HUMAN`. A 429 during a live demo run was absorbed this way
  and the run still passed every assertion.

The response schema is a discriminated union — one variant per signal with its own
required fields — not one flat envelope. The flat version was tried first and failed
against the real API: the model split a single `ACTION_REQUIRED` across two objects,
identity in one and `details` in the other, and the half without `action_type` was
dropped on validation. Per-variant `required` makes that shape impossible to emit.

**The demo is the working slice** — 14 steps, virtual clock, every step asserted:

```bash
python scripts/demo.py                        # embedded Postgres, brain from .env
python scripts/demo.py --brain gemini         # force the LLM brain
python scripts/demo.py --brain rules          # force the offline baseline
python scripts/demo.py --db postgresql://...  # against your own Postgres
```

Tests (spin up an embedded Postgres, run a real Procrastinate worker):

```bash
python -m pytest -q                      # 54 passed - fully offline (stubbed HTTP, embedded Postgres)
python -m pytest -m live -o addopts=""   # 2 more: hit the real Gemini API, need GEMINI_API_KEY
```

API + dashboard, and the durable worker:

```bash
python scripts/serve.py --embedded --seed   # bundled Postgres + demo data, http://127.0.0.1:8000
python scripts/serve.py                     # against DATABASE_URL from .env
python scripts/worker.py                    # executes scheduled wake-ups
```

---

## What the demo proves

| Step | Claim |
|---|---|
| 2 | Instance starts, initial outreach goes to the provider's number on file |
| 3 | Poller tick with nothing due is a no-op |
| 4 | "wrong number, it's actually 555-0199" writes an `entity_fact_version` row; the base `contact.phone` column is untouched |
| 5 | "records not available for 2 weeks" → `next_wake_at` ~14d out, clamped into the provider's business hours, `attempt_count` reset to 0, client told |
| 6 | Clock advances → instance wakes and calls the **corrected** number; no workflow code knew about the change |
| 7 | Three no-answers follow the declared `2d / 5d / 14d` ladder; the 4th exhausts it → `review_task`, `status = blocked`, client told a human is taking over |
| 8 | Staff resolve the task → instance unblocks, attempts reset, outreach resumes |
| 9 | Provider needs a signed HIPAA authorization → client notified, staff task raised; after resolution the provider is re-contacted *with* the auth |
| 9b | "$45 records fee, pay online first" → requirement parked, chasing stops, client told what is holding their records up, staff get a task carrying the amount and URL |
| 9c | Paralegal resolves it with `{"action": "paid", "reference": "chk-1041"}` → instance resumes and the next message to the provider cites the reference |
| 10 | Records received → client notified, instance completed; full audit trail printed |
| 11 | `client_checkin` runs on the same engine: 14-day cadence, and a concerning reply ("the pain is getting worse") raises a staff flag |
| 12 | Client updates their own email mid-workflow through the same generic write-back path |

---

## Design decisions worth calling out

**Fencing token instead of `locked_until` / `locked_by`.** The plan's schema had
manual lock columns. Procrastinate already row-locks jobs and we already lock the
instance with `SELECT ... FOR UPDATE`, so hand-rolled leases would be a third,
weaker lock. What is actually needed is *staleness* protection: a wake scheduled,
then superseded by a reschedule, must not fire. Each `schedule_wake` writes a fresh
`wake_token` on the instance and cancels the previous job; a job only executes if it
carries the current token. `locked_until`/`locked_by` were dropped in favour of
`wake_token` / `wake_job_id`.

**`next_wake_at` on the instance *and* a Procrastinate job.** The instance row is
the source of truth (queryable by the dashboard, drivable by a poller with a virtual
clock, and usable as a recovery sweep). The job is the production timer. They cannot
drift because the token binds them.

**Idempotency at the attempt, not the job.** Every attempt writes
`attempt_started` with a unique key (`{instance_id}:{wake_token}`) *before* any side
effect, inside the same transaction. A duplicate delivery logs `wake_skipped` and
does nothing.

**Facts are never destructive.** `entity_fact_version` is append-only; nothing
UPDATEs `contact.phone`. Reads go through `get_entity_field()`, which prefers the
latest applied fact and falls back to the base column. Below-threshold extractions
land as `proposed` plus a review task, unregistered fields as `rejected` — so the
same code path handles auto-apply, human confirmation, and refusal.

**`seq BIGSERIAL` on `event`.** Timestamps collide (same transaction, virtual
clock), which made the audit order nondeterministic. `seq` makes the timeline exact.

**The brain never imports a workflow.** `RuleBasedAgentBrain` takes a
`domain_rules_for(workflow_type)` callable and `GeminiAgentBrain` takes
`domain_signals_for(workflow_type)`; the registry supplies both. Neither brain has a
single workflow name in it.

**The brain interface is async.** `extract_signals` returns an awaitable because a
real brain does I/O. That was the only downstream change needed when the LLM landed —
one `await` in `Engine.handle_inbound`.

---

## When the other party wants something from us

Third parties rarely just say yes. They say *"there's a $45 records fee"*, *"submit it
through the Ciox portal"*, *"we don't take these by phone — email the request"*, *"fax
us the letter of representation"*. All of that is generic: the ball moves to our court,
a human has to act, and then the agent should resume **knowing what we did**.

That is the `ACTION_REQUIRED` signal:

```python
ActionRequired(
    action_type="payment",                     # payment | portal_submission | form | document | other
    summary="$45 records fee before release",
    details={"amount": "$45", "payee": "Mercy Health Information Management",
             "url": "pay.mercy.example"},      # what a paralegal actually acts on
    blocks_progress=True,
)
```

The Engine then, for any workflow:

1. parks the requirement on `instance.context["pending_requirement"]` so it survives a
   restart and is queryable;
2. stops chasing (`clear_wake`, `status = blocked`) — no point calling someone who is
   waiting on us;
3. raises a `review_task` named `action_required:payment` carrying the structured
   details, so the queue shows the amount and the payee rather than a sentence;
4. lets the workflow tell the client *why* their matter is paused (`on_generic_outcome`).

A human then resolves the task with `{"action": "paid", "reference": "chk-1041"}`
(`completed` / `done` / `paid` / `submitted` all count). The Engine moves the
requirement into `completed_requirements`, resets the attempt counter, and schedules an
immediate wake with reason `resume_after_requirement`. The next outreach can cite the
reference — the medical-records workflow appends *"payment required before release —
completed on our side (ref chk-1041)"*, so the provider cannot ask twice.

`blocks_progress=False` records an advisory requirement ("send an ID when convenient")
without stalling the retry ladder.

**"Don't call us, email us"** is handled a layer lower, as a fact rather than a
requirement. `preferred_channel` is a registry field like any other, and every
`ctx.send` resolves the channel through the fact store before picking an address. So
one `ENTITY_UPDATE` reroutes every future message to that contact, and the workflow —
which still says `channel="call"` — never changes. If the preferred channel has no
address on file, the requested channel is used instead, and the switch is logged as
`channel_overridden`.

Both brains handle these. The LLM extracts them with full particulars; the keyword
fallback recognises the common phrasings (fees, portals/vendors, "no phone requests"),
which matters because the fallback is exactly what runs during a provider outage.

---

## Adding a workflow

Two ways in, and the first one covers most of the work.

### 1. Build it from a form (no deploy)

Most outreach is the same machine: send to one contact, wait, chase on a ladder,
hand off to a human. That shape is the **outreach template**, and a workflow type is
a row configuring it - created at `/workflows`:

```
name              [ deposition_reminder      ]
message           [ Hi {contact_name}, reminding you about your deposition on {date}. ]
send by           [ sms ▾ ]
chase             [ 2 ] times, waiting [ 3 ] days between
give up after     [ 1 ] day of silence
on reply          (•) finish   ( ) repeat every [ 14 ] days
hand to a human if the reply mentions [ angry, complaint ]
```

`on reply` is the entire difference between the two workflows that shipped first:
`client_checkin` repeats every 14 days, `contact_update` finishes. Both are now rows,
seeded by `migrations/002_workflow_type.sql` - the Python modules are gone.

Then start one at `/instances/new`, entering the contact there:

```
workflow   [ deposition_reminder ▾ ]
contact    name [ Dana Reed ]  phone [ +15550999 ]  timezone [ America/Chicago ▾ ]
           reachable [ 09:00 ] to [ 17:00 ]
details    date = March 3rd
```

`{contact_name}` is filled in at send time; any other `{placeholder}` comes from those
`name = value` pairs, so one type serves many agents.

### 2. Write a template (a deploy)

Only when the shape itself is new - several parties, or side effects that differ per
signal. `medical_records_followup` is the example: it chases a provider, keeps the
client informed, and handles authorisations and fees. That stays a Python class of
about 140 lines, registered in `app/workflows/registry.py`:

```python
registry.register(MedicalRecordsFollowupWorkflow())
```

A new *template* (rather than a new type) is the same move: implement
`WorkflowDefinition`, parameterise it with a Pydantic spec, add it to `TEMPLATES`, and
every type built on it becomes a form.

Either way the workflow inherits retries, business-hours clamping, durable fenced
wakes, fact write-back, channel switching, the review queue, the API and the dashboard
- and the engine does not change.

---

## API surface (thin by design)

| Method | Path | |
|---|---|---|
| GET | `/` | dashboard: instances + review queue |
| GET | `/instances/{id}` | instance timeline page |
| GET | `/api/workflows` | registered types, their states, retry policies, domain signals |
| GET/POST | `/api/instances` | list (filter by `workflow_type`, `status`) / start one |
| GET | `/api/instances/{id}` | instance + full event timeline |
| POST | `/api/simulate/inbound` | feed a message/transcript to an instance |
| POST | `/api/simulate/tick` | run everything due now (also a recovery sweep) |
| GET | `/api/review-queue` | pending human tasks |
| POST | `/api/review-queue/{id}/resolve` | resolve one (`{"resolution": {"action": "retry"}}`) |
| GET | `/api/contacts/{id}/facts` | fact history for one contact |

---

## Out of scope in this slice (and where it would attach)

- **A node canvas.** The builder is a form over a template, not free-form states. A
  canvas would need the states themselves to become data and the engine to gain an
  interpreter; the `workflow_type` row is the shape it would emit.
- **Spec versioning.** Editing a type changes what its in-flight instances do at their
  next step. Flagged in the UI, not solved.
- **Full human-in-the-loop.** `review_task` rows, creation, listing and a minimal
  resolve path exist. Routing rules, assignment, SLAs and escalation ladders are
  **not** built — they belong in `app/review.py` and would not touch the Engine.
- **Real channels.** `MockChannel` only. `app/channels/base.py` is the seam;
  inbound webhooks would call `Engine.handle_inbound`.
- **Compliance / calling-hours enforcement.** `apply_compliance_window()` in
  `app/scheduling/constraints.py` is a pass-through stub. **Known gap** — TCPA and
  state calling-window rules must land there before any real dialing.
- **Multi-tenancy, authn/authz, rate limiting, PII redaction in the audit trail.**
  None present. The dashboard and API are unauthenticated.

## Known limitations

- The rule-based fallback is keyword matching: it will mis-read anything phrased
  unusually. Unmatched replies deliberately fall through to `NEEDS_HUMAN` rather
  than being silently dropped.
- The LLM brain calls Gemini once per inbound message, synchronously inside the
  inbound transaction. Fine at this volume; at scale it belongs in its own job so a
  slow provider cannot hold a database transaction open.
- No prompt-injection hardening. A hostile inbound message is passed to the model as
  data, and the guard rails above bound the damage (no arbitrary entity writes, no
  arbitrary delays), but a crafted message could still steer which signal is emitted.
  Confidence thresholds and the review queue are the current mitigation.
- Phone numbers are stored as extracted (`5550199`), not E.164-normalised.
- `Engine.run_due` processes due instances sequentially in one transaction; fine as
  a sweep, but the Procrastinate worker is the concurrent path.
- Python 3.11 (the plan said 3.12; nothing here needs 3.12 syntax).
