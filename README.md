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
1. Agent Brain          app/agent_brain.py       text -> [Signal]      (rule-based stub, swappable)
        |
        v
2. Engine               app/engine.py            workflow-AGNOSTIC orchestrator
   |- generic signals   RESCHEDULE / NO_ANSWER / ENTITY_UPDATE / NEEDS_HUMAN   -> handled here
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
| Human-in-the-loop | `app/review.py` | `review_task` + resolve path |
| Channels | `app/channels/` | `MockChannel`; real telephony/email plugs in here |
| Audit trail | `app/events.py` | append-only `event` table, monotonic `seq` |

`tests/test_extensibility.py` enforces the invariant twice: it runs a brand-new
workflow through the unmodified engine, and it greps `app/engine.py` and
`app/scheduling/scheduler.py` for any concrete workflow name.

---

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"    # Windows
# python -m venv .venv && .venv/bin/pip install -e ".[dev]"      # macOS/Linux

cp .env.example .env          # DATABASE_URL
docker compose up -d          # Postgres 16   (or skip: the demo/tests embed one)
```

**The demo is the working slice** — 12 steps, virtual clock, every step asserted:

```bash
python scripts/demo.py                       # embedded Postgres, nothing to install
python scripts/demo.py --db postgresql://...  # against your own Postgres
```

Tests (spin up an embedded Postgres, run a real Procrastinate worker):

```bash
python -m pytest -q          # 28 passed
```

API + dashboard, and the durable worker:

```bash
python scripts/serve.py      # http://127.0.0.1:8000   (uvicorn app.api.main:app also works off-Windows)
python scripts/worker.py     # executes scheduled wake-ups
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
`domain_rules_for(workflow_type)` callable; the registry supplies each workflow's own
`keyword_rules`. An LLM implementation would instead build a tool schema from each
workflow's `domain_signals` — same seam, one new file.

---

## Adding workflow #3

One file + one line:

```python
# app/workflows/court_deadline.py
class CourtDeadlineWorkflow(BaseWorkflow):
    workflow_type = "court_deadline_confirmation"
    initial_state = "awaiting_confirmation"
    retry_policy = {"no_answer": {"schedule": ["1d", "3d"]}}
    domain_signals = [DeadlineConfirmed]
    keyword_rules = [rule("deadline_confirmed", r"...", lambda m, t, c: DeadlineConfirmed())]

    async def on_wake(self, instance, ctx): ...
    async def handle_domain_signal(self, instance, signal, ctx): ...
```

```python
# app/workflows/registry.py
registry.register(CourtDeadlineWorkflow())
```

It gets retries, business-hours clamping, durable wake-ups, fact write-back, the
review queue, the audit trail, the API and the dashboard for free. Workflows reach
the platform only through `WorkflowContext` (`send`, `schedule_wake_in`,
`transition`, `complete`, `create_review_task`, `log`, `contact_field`) — they never
touch the DB, scheduler or channels directly.

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

- **Visual workflow builder.** Not built. The manifest shape (states + declarative
  retry policy + domain signal handlers) is what a canvas would emit; moving from
  Python classes to YAML is a loader change behind `WorkflowRegistry`, nothing else.
- **Full human-in-the-loop.** `review_task` rows, creation, listing and a minimal
  resolve path exist. Routing rules, assignment, SLAs and escalation ladders are
  **not** built — they belong in `app/review.py` and would not touch the Engine.
- **Real LLM brain.** Keyword stub only. See "the brain never imports a workflow".
- **Real channels.** `MockChannel` only. `app/channels/base.py` is the seam;
  inbound webhooks would call `Engine.handle_inbound`.
- **Compliance / calling-hours enforcement.** `apply_compliance_window()` in
  `app/scheduling/constraints.py` is a pass-through stub. **Known gap** — TCPA and
  state calling-window rules must land there before any real dialing.
- **Multi-tenancy, authn/authz, rate limiting, PII redaction in the audit trail.**
  None present. The dashboard and API are unauthenticated.

## Known limitations

- The rule-based brain is keyword matching: it will mis-read anything phrased
  unusually. Unmatched replies deliberately fall through to `NEEDS_HUMAN` rather
  than being silently dropped.
- Phone numbers are stored as extracted (`5550199`), not E.164-normalised.
- `Engine.run_due` processes due instances sequentially in one transaction; fine as
  a sweep, but the Procrastinate worker is the concurrent path.
- Python 3.11 (the plan said 3.12; nothing here needs 3.12 syntax).
