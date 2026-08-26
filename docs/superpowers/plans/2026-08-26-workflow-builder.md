# Workflow Templates and Builder UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse `client_checkin` and `contact_update` into one parameterised template whose workflow types are database rows, and let a person create a type and start an agent from the UI.

**Architecture:** A *template* is code (a parameterised `WorkflowDefinition`); a *workflow type* is data (a row naming a template plus a validated spec). The registry serves code-defined and database-defined types side by side. The engine does not change.

**Tech Stack:** Python 3.11, Pydantic v2, SQLAlchemy async, FastAPI + Jinja2, Postgres, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-workflow-builder-design.md`

## Global Constraints

- The engine (`app/engine.py`) and scheduler must not change. `tests/test_extensibility.py` asserts they name no workflow.
- `registry.get()` stays synchronous; refresh is a separate async call.
- Retries are entered as `retry_count` + `retry_interval_days` and expanded to the ladder the engine already understands.
- `tests/test_client_checkin.py` and `tests/test_contact_update.py` must pass **unchanged** — they are the parity proof.
- `medical_records_followup` stays code-defined.

---

### Task 1: Outreach spec and template

**Files:**
- Create: `app/workflows/templates/__init__.py`, `app/workflows/templates/outreach.py`
- Test: `tests/test_outreach_template.py`

**Interfaces:**
- Produces: `OutreachSpec` (Pydantic), `OutreachTemplate(workflow_type: str, spec: OutreachSpec)`, signals `Acknowledged`, `Flagged(reason: str)`, `TEMPLATES: dict[str, type]`.
- Consumes: `BaseWorkflow`, `WorkflowContext`, `rule`, `KeywordRule` from existing modules.

- [ ] **Step 1: Write the failing test**

```python
from app.workflows.templates.outreach import OutreachSpec, OutreachTemplate

def test_ladder_is_expanded_from_count_and_interval():
    spec = OutreachSpec(message="hi", retry_count=3, retry_interval_days=2)
    assert spec.ladder() == ["2d", "2d", "2d"]
    wf = OutreachTemplate("demo", spec)
    assert wf.retry_policy == {"no_answer": {"schedule": ["2d", "2d", "2d"]}}
    assert wf.response_deadline == "2d"

def test_repeat_requires_a_cadence():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        OutreachSpec(message="hi", on_reply="repeat")
```

- [ ] **Step 2: Run it, expect ModuleNotFoundError**

Run: `.venv/Scripts/python -m pytest tests/test_outreach_template.py -q`

- [ ] **Step 3: Implement spec + template**

`OutreachSpec` fields: `message`, `channel`, `recipient_key`, `retry_count`, `retry_interval_days`, `response_deadline_days`, `on_reply`, `repeat_every_days`, `reminder_days`, `ack_phrases`, `escalate_keywords`, `escalate_reason`, `description`. `model_validator` rejects `on_reply="repeat"` without `repeat_every_days`. `ladder()` returns `[f"{interval}d"] * count`. `keyword_rules()` builds `Acknowledged` / `Flagged` rules from the phrase lists. `prompt_notes()` returns a sentence naming the escalation topics for the LLM.

`OutreachTemplate.on_wake` renders `message` with `{contact_name}` and instance context, sends on `spec.channel` to `context[spec.recipient_key]`, arms the reminder if set. `handle_domain_signal`: `Acknowledged` → complete, or transition + `schedule_wake_in(repeat_every)`; `Flagged` → review task. `resolution_options` exposes `acknowledged`.

- [ ] **Step 4: Run tests, expect pass**
- [ ] **Step 5: Commit** — `feat: outreach template covering send/wait/retry workflows`

---

### Task 2: workflow_type table and repository

**Files:**
- Create: `migrations/002_workflow_type.sql`, `app/workflows/types.py`
- Modify: `app/db/models.py`
- Test: `tests/test_workflow_types.py`

**Interfaces:**
- Produces: `WorkflowTypeRow` model; `async list_types(session)`, `async upsert_type(session, name, template, spec, description, now)`, `async load_definitions(session) -> dict[str, WorkflowDefinition]`, `async types_version(session) -> tuple[int, datetime | None]`.

- [ ] **Step 1: Write the failing test** — round-trip a type through `upsert_type` / `load_definitions`, assert the built definition's `retry_policy` matches the spec's ladder, and that an unknown template name raises `KeyError`.
- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Write the migration and repository** — table per spec; `load_definitions` validates each row's spec through `TEMPLATES[row.template]`'s spec model and constructs the definition.
- [ ] **Step 4: Run tests, expect pass**
- [ ] **Step 5: Commit** — `feat: persist workflow types as rows`

---

### Task 3: Registry serves code and database types

**Files:**
- Modify: `app/workflows/registry.py`, `app/runtime.py`, `app/engine.py` (call site only — `ensure_fresh`)
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `WorkflowRegistry.register(definition)`, `.get(name)` (sync), `async .ensure_fresh(session, ttl=30)`, `async .reload(session)`, `.types()`, `.is_code_defined(name)`.

- [ ] **Step 1: Write the failing test** — a code type and a DB type both resolve through `get()`; a type inserted after load appears after `reload()`; `ensure_fresh` does nothing inside the TTL.
- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Implement** — registry holds `_code: dict` and `_db: dict` plus `_loaded_at`. `Engine.start_instance` and `handle_inbound` call `await self.registry.ensure_fresh(session)` before `get`.
- [ ] **Step 4: Run tests, expect pass**
- [ ] **Step 5: Commit** — `feat: registry resolves database-defined workflow types`

---

### Task 4: Seed the two types and delete the modules

**Files:**
- Modify: `migrations/002_workflow_type.sql` (seed rows), `app/workflows/registry.py` (`default_registry`), `tests/test_llm_brain.py:18` (import)
- Delete: `app/workflows/client_checkin.py`, `app/workflows/contact_update.py`

- [ ] **Step 1: Run the parity suites and record they pass** — `pytest tests/test_client_checkin.py tests/test_contact_update.py -q`
- [ ] **Step 2: Add seed rows** reproducing today's behaviour: `client_checkin` (`on_reply=repeat`, `repeat_every_days=14`, ladder `1d×2`, deadline 2d, escalate keywords worse/pain/hospital/…), `contact_update` (`on_reply=complete`, ladder `1d×2`, deadline 2d).
- [ ] **Step 3: Delete both modules and their registry lines; update the one import in `test_llm_brain.py` to the template signals**
- [ ] **Step 4: Run the parity suites again — they must pass unchanged**
- [ ] **Step 5: Run the whole suite**
- [ ] **Step 6: Commit** — `refactor: client_checkin and contact_update become template rows`

---

### Task 5: API for types and for starting an agent with a contact

**Files:**
- Modify: `app/api/main.py`
- Test: `tests/test_builder_api.py`

**Interfaces:**
- Produces: `GET /api/workflow-types`, `POST /api/workflow-types`, `POST /api/agents` (creates contact + optional case + instance).

- [ ] **Step 1: Write the failing test** — POST a type, assert it appears in `/api/workflows`; POST `/api/agents` with contact fields, assert a contact was created and an instance started and a message sent to that phone.
- [ ] **Step 2: Run it, expect 404s**
- [ ] **Step 3: Implement** — validate the spec through the template's model, `upsert_type`, `reload`. `/api/agents` creates `Contact` (+ `CaseRecord` when `matter_type` given), builds context from `recipient_key`, calls `start_instance`.
- [ ] **Step 4: Run tests, expect pass**
- [ ] **Step 5: Commit** — `feat: create workflow types and start agents over the API`

---

### Task 6: Builder and start-an-agent pages

**Files:**
- Create: `app/api/templates/workflows.html`, `app/api/templates/new_instance.html`
- Modify: `app/api/main.py` (routes), `app/api/templates/base.html` (nav)
- Test: `tests/test_builder_api.py` (add page tests)

- [ ] **Step 1: Write the failing test** — `/workflows` lists both kinds with a badge; posting the builder form creates a type; `/instances/new` posts name/phone and lands on the new instance page.
- [ ] **Step 2: Run it, expect failure**
- [ ] **Step 3: Build the pages** — form fields exactly as the spec's sketch; retries as two number inputs; `on reply` radio with a days box; escalation keywords comma-separated.
- [ ] **Step 4: Run the whole suite**
- [ ] **Step 5: Commit** — `feat: workflow builder and start-an-agent pages`

---

### Task 7: Documentation

**Files:** Modify `README.md`

- [ ] **Step 1: Rewrite the "Adding workflow #3" section** — creating a type is now a form, not a file; code templates remain for workflows with real branching.
- [ ] **Step 2: Note the accepted risk** — editing a spec affects in-flight instances.
- [ ] **Step 3: Run the whole suite and commit** — `docs: workflow types are data`
