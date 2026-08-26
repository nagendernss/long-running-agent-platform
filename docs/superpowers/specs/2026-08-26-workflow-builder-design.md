# Workflow templates and a builder UI

**Date:** 2026-08-26
**Status:** approved for planning

## Problem

`client_checkin` and `contact_update` are two Python modules describing the same
machine. Both send one message to one contact, wait for a reply, retry on a ladder,
and hand off to a human when the ladder runs out. The only real difference is what a
positive reply means:

| | `client_checkin` | `contact_update` |
|---|---|---|
| positive reply | reschedule on a 14-day cadence | complete |
| escalation | concerning content raises a review task | none |
| ladder / deadline | `1d, 3d` / `2d` | `1d, 3d` / `2d` |

That is a parameter, not a module. Meanwhile every new use case of this shape needs a
Python file, a registry line and a deploy, which puts the platform out of reach of the
people who actually know what the outreach should say.

`medical_records_followup` is not of this shape - three parties, and each domain
signal has its own side effects - and stays as code.

## Goals

1. One parameterised template covers both existing workflows, with their behaviour
   provably unchanged.
2. New workflow types are created from the UI, no deploy.
3. Agents are started from the UI, including entering the contact's details there.
4. The engine does not change.

## Non-goals

- A node canvas. The spec is a form; the shape is a template, not free-form states.
- Templating `medical_records_followup`.
- Versioning specs. Editing a spec affects in-flight instances; documented, not solved.

## Design

### Template (code) vs workflow type (data)

A **template** is a parameterised `WorkflowDefinition` implementation. A **workflow
type** is a row naming a template plus a spec.

```python
class OutreachTemplate(BaseWorkflow):
    template_name = "outreach"

    def __init__(self, workflow_type: str, spec: OutreachSpec):
        self.workflow_type = workflow_type
        self.spec = spec
        self.retry_policy = {"no_answer": {"schedule": spec.ladder()}}
        self.response_deadline = f"{spec.response_deadline_days}d"
        self.keyword_rules = spec.keyword_rules()
```

### The spec

Stored as the operator entered it, expanded at load, so the builder form round-trips
exactly and the engine still sees the ladder it already understands.

```python
class OutreachSpec(BaseModel):
    message: str                                   # "{contact_name}" placeholders
    channel: Literal["call", "sms", "email"] = "sms"
    recipient_key: str = "target_contact_id"

    retry_count: int = 2                           # how many times to chase
    retry_interval_days: int = 2                   # wait between attempts
    response_deadline_days: int = 2                # silence that counts as no answer

    on_reply: Literal["complete", "repeat"] = "complete"
    repeat_every_days: int | None = None           # required iff on_reply == "repeat"
    reminder_days: int | None = None               # one nudge before the ladder

    ack_phrases: list[str] = [...]                 # what reads as a positive reply
    escalate_keywords: list[str] = []              # concerning content
    escalate_reason: str = "flagged"

    def ladder(self) -> list[str]:
        return [f"{self.retry_interval_days}d"] * self.retry_count
```

`on_reply` is the whole difference between the two existing workflows: `repeat` with
`repeat_every_days=14` is the check-in, `complete` is the contact update.

### Signals

The template owns two domain signals, `ACKNOWLEDGED` and `FLAGGED`, shared by every
type built on it. Routing is per instance (`workflow_type` -> definition), so sharing
signal classes across types is safe. Keyword rules are generated from `ack_phrases`
and `escalate_keywords`. For the LLM path, `WorkflowDefinition` gains an optional
`prompt_notes`, appended to the extraction prompt, carrying the type's own sense of
what counts as concerning.

### Persistence

```sql
CREATE TABLE workflow_type (
    name        TEXT PRIMARY KEY,
    template    TEXT NOT NULL,
    spec        JSONB NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
```

Migration `002` creates the table and seeds `client_checkin` and `contact_update` as
rows whose specs reproduce today's behaviour. Both Python modules are then deleted.

### Registry

`get()` stays synchronous - it is on every hot path. It resolves code-defined types
first, then an in-memory cache of database types.

- `await ensure_fresh(session)` refreshes on a 30-second TTL, costing one
  `SELECT max(updated_at), count(*)` at most twice a minute per process.
- The API calls `reload()` straight after a create or edit, so the builder is instant
  for the person using it.
- `medical_records_followup` remains registered in code, so the registry demonstrably
  serves both kinds.

### Engine

Unchanged. It only ever calls `registry.get(instance.workflow_type)`.

### UI

**`/workflows`** - lists types with a code/template badge, and a form to create one:

```
name              [ client_checkin            ]
template          [ outreach                ▾ ]
message           [ Hi {contact_name}, quick check-in… ]
channel           [ sms ▾ ]
retries           [ 2 ] times, waiting [ 2 ] days between
response deadline [ 2 ] days
on reply          ( ) finish   (•) repeat every [ 14 ] days
escalate if reply mentions [ pain, hospital, worse ]
```

Template-backed types are editable; code-defined ones are read-only.

**`/instances/new`** - starts an agent, collecting the contact inline:

```
workflow   [ client_checkin ▾ ]
contact    name [ Jane Okafor ]  phone [ +1555… ]  email [ … ]
           timezone [ America/New_York ▾ ]  hours [ 09:00 ]-[ 17:00 ]
case       matter type [ personal_injury ]        (optional)
```

Submitting creates the contact (and case if given), then starts the instance with a
context built from the spec's `recipient_key`.

## Testing

- **Parity.** `tests/test_client_checkin.py` and `tests/test_contact_update.py` run
  unchanged against the seeded rows. They drive by workflow-type string and never
  import the classes, so passing them is direct proof the behaviour survived.
- Spec validation: `on_reply="repeat"` without `repeat_every_days` is rejected;
  `retry_count=0` means straight to a human.
- Registry: code and database types together, TTL refresh, reload after mutation,
  unknown type still raises.
- Builder round-trip: create a type over the API, it appears in `/api/workflows`,
  start an instance of it, it runs and retries on the generated ladder.
- `tests/test_extensibility.py` continues to assert the engine names no workflow.

## Risks accepted

- A type created in the API process is invisible to a worker for up to 30 seconds.
  New types are not started mid-flight, so this is tolerable.
- Editing a spec changes behaviour for in-flight instances of that type. Documented
  in the UI; spec versioning is real scope and not justified yet.
- `tests/test_llm_brain.py` imports `CheckinOk` / `ClientFlag` directly and moves to
  the shared template signals.

## Shape of the change

| Area | Change |
|---|---|
| `app/workflows/templates/outreach.py` | new - template + spec |
| `app/workflows/types.py` | new - load, create, update DB types |
| `app/workflows/registry.py` | rewrite - code + database, TTL |
| `app/workflows/client_checkin.py`, `contact_update.py` | deleted |
| `migrations/002_workflow_type.sql` | new - table + seeds |
| `app/api/main.py`, templates | `/workflows`, `/instances/new` |
| `app/llm_brain.py` | one line - `prompt_notes` |
| `app/engine.py` | none |
