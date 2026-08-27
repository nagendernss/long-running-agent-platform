"""Thin read-mostly API + server-rendered dashboard. Deliberately generic: every
endpoint works for any workflow_type - nothing here knows a workflow by name."""
from __future__ import annotations

import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CaseRecord, Contact, EntityFactVersion, ReviewTask, WorkflowInstance
from app.api.timeline import build_rounds
from app.clock import OffsetClock, parse_duration
from app.events import list_events
from app.review import list_pending_review_tasks
from app.workflows.types import list_types, upsert_type
from app.voice.queue import offer_next
from app.voice.repository import get_call, waiting_calls
from app.voice.session import VoiceCallSession, miss_call
from app.runtime import Runtime, build_runtime, ensure_schema, get_runtime
from app.config import get_settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:  # an entrypoint (scripts/serve.py) may have built one already, e.g. on a
        get_runtime()  # time-travel clock - don't replace it
        yield
        return
    except RuntimeError:
        pass
    settings = get_settings()
    await ensure_schema(settings)
    runtime = await build_runtime(settings)
    try:
        yield
    finally:
        await runtime.close()


app = FastAPI(title="HelloCounsel Agent Platform", lifespan=lifespan)


def rt() -> Runtime:
    return get_runtime()


async def session(runtime: Runtime = Depends(rt)) -> AsyncSession:
    async with runtime.session_factory() as s:
        yield s


# ---------------------------------------------------------------- schemas
class StartInstanceIn(BaseModel):
    workflow_type: str
    case_id: uuid.UUID | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class InboundIn(BaseModel):
    instance_id: uuid.UUID
    text: str
    channel: str = "sms"


class WorkflowTypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    template: str = "outreach"
    description: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)


class NewAgentIn(BaseModel):
    """Start an agent, entering the contact here rather than needing one to exist."""

    workflow_type: str
    contact_name: str
    phone: str | None = None
    email: str | None = None
    timezone: str = "America/New_York"
    business_start: str = "09:00"
    business_end: str = "17:00"
    role: str | None = None      # defaults to whoever this workflow contacts
    matter_type: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class AdvanceIn(BaseModel):
    duration: str | None = None      # "2h", "14d" - omit to jump to the next due wake
    run_due: bool = True


class ResolveIn(BaseModel):
    resolution: dict[str, Any] = Field(default_factory=dict)
    resolved_by: str = "api"


def instance_json(i: WorkflowInstance) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "workflow_type": i.workflow_type,
        "case_id": str(i.case_id) if i.case_id else None,
        "state": i.state,
        "status": i.status,
        "attempt_count": i.attempt_count,
        "next_wake_at": i.next_wake_at.isoformat() if i.next_wake_at else None,
        "wake_reason": i.wake_reason,
        "context": i.context,
        "updated_at": i.updated_at.isoformat() if i.updated_at else None,
    }


# ---------------------------------------------------------------- JSON API
@app.get("/api/workflows")
async def api_workflows(runtime: Runtime = Depends(rt)):
    return {
        wt: {
            "initial_state": runtime.registry.get(wt).initial_state,
            "retry_policy": runtime.registry.get(wt).retry_policy,
            "domain_signals": [s.model_fields["type"].default for s in runtime.registry.get(wt).domain_signals],
        }
        for wt in runtime.registry.types()
    }


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


@app.get("/api/workflow-types")
async def api_workflow_types(s: AsyncSession = Depends(session), runtime: Runtime = Depends(rt)):
    """Every type the platform can run: code-defined ones and the rows people built."""
    rows = {r.name: r for r in await list_types(s)}
    out = []
    for name in runtime.registry.types():
        row = rows.get(name)
        definition = runtime.registry.get(name)
        out.append({
            "name": name,
            "kind": "code" if runtime.registry.is_code_defined(name) else "template",
            "template": row.template if row else None,
            "description": row.description if row else _first_line(definition.__doc__),
            "spec": row.spec if row else None,
            "editable": row is not None,
            "retry_policy": definition.retry_policy,
        })
    return out


@app.post("/api/workflow-types", status_code=201)
async def api_create_workflow_type(body: WorkflowTypeIn, runtime: Runtime = Depends(rt)):
    if runtime.registry.is_code_defined(body.name):
        raise HTTPException(409, f"{body.name} is defined in code and cannot be edited here")
    async with runtime.session_factory() as s, s.begin():
        try:
            row = await upsert_type(
                s, name=body.name, template=body.template, spec=body.spec,
                description=body.description, now=runtime.clock.now(),
            )
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from None
        except PydanticValidationError as exc:
            # pydantic puts the original exception in ctx, which will not serialise -
            # the person filling in the form wants the sentence anyway.
            raise HTTPException(422, [
                {"field": ".".join(str(p) for p in e["loc"]) or "spec", "message": e["msg"]}
                for e in exc.errors(include_url=False)
            ]) from None
        await runtime.registry.reload(s)   # the builder should feel instant
        return {"name": row.name, "template": row.template, "spec": row.spec}


@app.post("/api/agents", status_code=201)
async def api_start_agent(body: NewAgentIn, runtime: Runtime = Depends(rt)):
    """Create the contact (and a case if a matter type is given), then start one
    instance pointed at it."""
    async with runtime.session_factory() as s, s.begin():
        await runtime.registry.ensure_fresh(s)
        if body.workflow_type not in runtime.registry.types():
            raise HTTPException(400, f"unknown workflow_type: {body.workflow_type}")
        if not (body.phone or body.email):
            raise HTTPException(422, "a contact needs a phone number or an email address")

        now = runtime.clock.now()
        definition = runtime.registry.get(body.workflow_type)
        role = body.role or getattr(definition, "contact_role", "client")
        contact = Contact(
            id=uuid.uuid4(), name=body.contact_name, role=role, phone=body.phone or None,
            email=body.email or None, timezone=body.timezone,
            business_hours={"start": body.business_start, "end": body.business_end}, created_at=now,
        )
        s.add(contact)
        await s.flush()

        case = None
        if body.matter_type:
            case = CaseRecord(id=uuid.uuid4(), client_contact_id=contact.id,
                              matter_type=body.matter_type, created_at=now)
            s.add(case)
            await s.flush()

        recipient_key = getattr(getattr(definition, "spec", None), "recipient_key", "target_contact_id")
        context = {
            "target_contact_id": str(contact.id),
            recipient_key: str(contact.id),
            **body.context,
        }
        instance = await runtime.engine.start_instance(
            s, body.workflow_type, case_id=case.id if case else None, context=context
        )
        return {**instance_json(instance), "contact_id": str(contact.id)}


@app.get("/api/instances")
async def api_instances(
    workflow_type: str | None = None, status: str | None = None, s: AsyncSession = Depends(session)
):
    stmt = select(WorkflowInstance).order_by(WorkflowInstance.created_at.desc())
    if workflow_type:
        stmt = stmt.where(WorkflowInstance.workflow_type == workflow_type)
    if status:
        stmt = stmt.where(WorkflowInstance.status == status)
    return [instance_json(i) for i in (await s.execute(stmt)).scalars().all()]


@app.get("/api/instances/{instance_id}")
async def api_instance(instance_id: uuid.UUID, s: AsyncSession = Depends(session)):
    inst = await s.get(WorkflowInstance, instance_id)
    if inst is None:
        raise HTTPException(404, "instance not found")
    return {
        **instance_json(inst),
        "timeline": [
            {"seq": e.seq, "type": e.type, "payload": e.payload, "at": e.created_at.isoformat() if e.created_at else None}
            for e in await list_events(s, instance_id)
        ],
    }


@app.post("/api/instances", status_code=201)
async def api_start_instance(body: StartInstanceIn, runtime: Runtime = Depends(rt)):
    if body.workflow_type not in runtime.registry.types():
        raise HTTPException(400, f"unknown workflow_type: {body.workflow_type}")
    async with runtime.session_factory() as s, s.begin():
        inst = await runtime.engine.start_instance(
            s, body.workflow_type, case_id=body.case_id, context=body.context
        )
        return instance_json(inst)


@app.post("/api/simulate/inbound")
async def api_simulate_inbound(body: InboundIn, runtime: Runtime = Depends(rt)):
    """Feed a message/transcript to an instance as if it arrived on a real channel."""
    async with runtime.session_factory() as s, s.begin():
        try:
            signals = await runtime.engine.handle_inbound(s, body.instance_id, body.text, channel=body.channel)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        inst = await s.get(WorkflowInstance, body.instance_id)
        return {"signals": [sig.model_dump() for sig in signals], "instance": instance_json(inst)}


@app.post("/api/simulate/tick")
async def api_tick(runtime: Runtime = Depends(rt)):
    """Run every instance whose next_wake_at is due (the Procrastinate worker does this
    in production; exposed here for manual testing and as a recovery sweep)."""
    async with runtime.session_factory() as s, s.begin():
        return [{"instance_id": str(i), "result": r} for i, r in await runtime.engine.run_due(s)]


def ladder_sizes(runtime: Runtime) -> dict[str, int]:
    """How many retries each workflow declares - so "attempts 2" can be shown as
    "2 of 2" and a blocked instance reads as finished, not stuck."""
    return {
        wt: len(((runtime.registry.get(wt).retry_policy or {}).get("no_answer") or {}).get("schedule") or [])
        for wt in runtime.registry.types()
    }


async def resolution_options(runtime: Runtime, s: AsyncSession, tasks: list[ReviewTask]) -> dict[str, list[dict[str, str]]]:
    """task id -> the domain outcomes its workflow lets a reviewer record."""
    options: dict[str, list[dict[str, str]]] = {}
    for task in tasks:
        inst = await s.get(WorkflowInstance, task.instance_id) if task.instance_id else None
        if inst is None:
            continue
        definition = runtime.registry.get(inst.workflow_type)
        options[str(task.id)] = list(definition.outcomes_for(inst))
    return options


def clock_state(runtime: Runtime) -> dict[str, Any]:
    clock = runtime.clock
    travelling = isinstance(clock, OffsetClock)
    return {
        "now": clock.now().isoformat(),
        "time_travel": travelling,
        "offset_seconds": int(clock.offset.total_seconds()) if travelling else 0,
    }


@app.get("/api/clock")
async def api_clock(runtime: Runtime = Depends(rt)):
    return clock_state(runtime)


@app.post("/api/simulate/advance")
async def api_advance(body: AdvanceIn, runtime: Runtime = Depends(rt)):
    """Push the dev server's clock forward so a 14-day wait can be watched in a second.

    With no duration, jumps to just past the earliest pending wake - the usual way to
    step an instance forward one attempt at a time.
    """
    clock = runtime.clock
    if not isinstance(clock, OffsetClock):
        raise HTTPException(400, "server is running on the real clock; start it with --time-travel")

    if body.duration:
        clock.advance(parse_duration(body.duration))
        jumped_to = None
    else:
        async with runtime.session_factory() as s:
            stmt = (
                select(WorkflowInstance.next_wake_at)
                .where(WorkflowInstance.status == "active", WorkflowInstance.next_wake_at.is_not(None))
                .order_by(WorkflowInstance.next_wake_at.asc())
                .limit(1)
            )
            nxt = (await s.execute(stmt)).scalar_one_or_none()
        if nxt is None:
            return {**clock_state(runtime), "fired": [], "note": "nothing is scheduled"}
        jumped_to = nxt.isoformat()
        clock.advance_to(nxt + timedelta(minutes=1))

    fired = []
    if body.run_due:
        async with runtime.session_factory() as s, s.begin():
            fired = [{"instance_id": str(i), "result": r} for i, r in await runtime.engine.run_due(s)]
    return {**clock_state(runtime), "jumped_to": jumped_to, "fired": fired}


@app.post("/api/simulate/clock/reset")
async def api_clock_reset(runtime: Runtime = Depends(rt)):
    if not isinstance(runtime.clock, OffsetClock):
        raise HTTPException(400, "server is running on the real clock")
    runtime.clock.reset()
    return clock_state(runtime)


@app.get("/api/review-queue")
async def api_review_queue(s: AsyncSession = Depends(session)):
    tasks = await list_pending_review_tasks(s)
    options = await resolution_options(get_runtime(), s, tasks)
    return [
        {
            "id": str(t.id),
            "instance_id": str(t.instance_id),
            "reason": t.reason,
            "suggested_options": t.suggested_options,
            "resolution_options": options.get(str(t.id), []),
            "context_snapshot": t.context_snapshot,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]


@app.post("/api/review-queue/{task_id}/resolve")
async def api_resolve(task_id: uuid.UUID, body: ResolveIn, runtime: Runtime = Depends(rt)):
    async with runtime.session_factory() as s, s.begin():
        try:
            task = await runtime.engine.resolve_review_task(s, task_id, body.resolution, resolved_by=body.resolved_by)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"id": str(task.id), "status": task.status, "resolution": task.resolution}


@app.get("/api/contacts/{contact_id}/facts")
async def api_contact_facts(contact_id: uuid.UUID, s: AsyncSession = Depends(session)):
    stmt = (
        select(EntityFactVersion)
        .where(EntityFactVersion.entity_type == "contact", EntityFactVersion.entity_id == contact_id)
        .order_by(EntityFactVersion.created_at.desc())
    )
    return [
        {
            "field": f.field, "old_value": f.old_value, "new_value": f.new_value,
            "confidence": f.confidence, "status": f.status,
            "at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in (await s.execute(stmt)).scalars().all()
    ]


# ---------------------------------------------------------------- calls
def call_json(call) -> dict[str, Any]:
    return {
        "id": str(call.id),
        "instance_id": str(call.instance_id) if call.instance_id else None,
        "status": call.status,
        "to": call.to_address,
        "opening": call.opening,
        "goal": call.goal,
        "transcript": call.transcript or [],
        "created_at": call.created_at.isoformat() if call.created_at else None,
        "ringing_since": call.ringing_since.isoformat() if call.ringing_since else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
    }


@app.get("/api/phone")
async def api_phone(runtime: Runtime = Depends(rt)):
    """What the phone should be doing: at most one call, plus how many are behind it.

    Several instances can come due at once, but one person answers the phone, so the
    calls queue rather than all ringing together.
    """
    async with runtime.session_factory() as s, s.begin():
        state = await offer_next(s, runtime.clock)
        return {
            "call": call_json(state.offered) if state.offered else None,
            "waiting": state.waiting,
            "busy": state.busy,
        }


@app.get("/api/calls")
async def api_calls(status: str | None = None, s: AsyncSession = Depends(session)):
    """Every call in a state, unfiltered. The phone page uses /api/phone instead."""
    if status in (None, "waiting", "queued", "ringing"):
        calls = await waiting_calls(s)
        if status in ("queued", "ringing"):
            calls = [c for c in calls if c.status == status]
        return [call_json(c) for c in calls]
    from sqlalchemy import select as _select

    from app.db.models import CallRow

    stmt = _select(CallRow).where(CallRow.status == status).order_by(CallRow.created_at.desc()).limit(50)
    return [call_json(c) for c in (await s.execute(stmt)).scalars().all()]


@app.get("/api/calls/{call_id}")
async def api_call(call_id: uuid.UUID, s: AsyncSession = Depends(session)):
    call = await get_call(s, call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    return call_json(call)


@app.post("/api/calls/{call_id}/miss")
async def api_miss_call(call_id: uuid.UUID, runtime: Runtime = Depends(rt)):
    """Declined, or nobody picked up. Goes through the same door as a real no-answer."""
    await miss_call(runtime.session_factory, runtime.clock, runtime.engine, call_id)
    return {"id": str(call_id), "status": "missed"}


@app.websocket("/ws/call/{call_id}")
async def call_socket(websocket: WebSocket, call_id: uuid.UUID):
    """Audio up, sentences down.

    The browser decides when a turn ended (it can hear the silence); the server does
    the transcribing and the thinking. A socket that drops is a hang-up with whatever
    was said so far - the call is over either way, and the transcript is still evidence.
    """
    runtime = get_runtime()
    await websocket.accept()
    voice = runtime.voice
    if voice is None:
        await websocket.send_json({"type": "error", "text": "voice calls are not enabled on this server"})
        await websocket.close()
        return

    call_session = VoiceCallSession(
        call_id=call_id, stt=voice.stt, agent=voice.agent,
        session_factory=runtime.session_factory, clock=runtime.clock, engine=runtime.engine,
    )
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if (data := message.get("bytes")) is not None:
                turn = await call_session.on_utterance(data)
                await websocket.send_json({
                    "type": "transcript", "who": "contact", "text": turn.heard,
                    "stt_ms": turn.stt_ms, "bytes": len(data),
                })
                await websocket.send_json({
                    "type": "speak", "text": turn.say, "done": turn.done,
                    "source": turn.source, "agent_ms": turn.agent_ms,
                })
                if turn.done:
                    await call_session.on_hangup(reason=turn.reason or "agent ended the call")
                    break
                continue
            payload = json.loads(message.get("text") or "{}")
            if payload.get("type") == "answer":
                opening = await call_session.on_answer()
                await websocket.send_json({"type": "speak", "text": opening, "done": False, "source": "opening"})
            elif payload.get("type") == "hangup":
                await call_session.on_hangup(reason="they hung up")
                break
    except WebSocketDisconnect:
        await call_session.on_hangup(reason="the line dropped")
    except Exception:
        log.exception("call %s failed", call_id)
        await call_session.on_hangup(reason="call failed", status="failed")
    finally:
        await call_session.on_hangup(reason="socket closed")
        with contextlib.suppress(Exception):
            await websocket.close()


@app.get("/phone", response_class=HTMLResponse)
async def phone_page(request: Request, runtime: Runtime = Depends(rt)):
    return TEMPLATES.TemplateResponse(
        request, "phone.html", {"clock": clock_state(runtime), "voice_enabled": runtime.voice is not None}
    )


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, s: AsyncSession = Depends(session), runtime: Runtime = Depends(rt)):
    instances = (await s.execute(select(WorkflowInstance).order_by(WorkflowInstance.created_at.desc()))).scalars().all()
    reviews = await list_pending_review_tasks(s)
    return TEMPLATES.TemplateResponse(
        request, "dashboard.html",
        {"instances": instances, "reviews": reviews, "workflow_types": runtime.registry.types(),
         "clock": clock_state(runtime), "ladders": ladder_sizes(runtime),
         "options": await resolution_options(runtime, s, reviews)},
    )


def _form_error(exc: Exception) -> str:
    """One readable sentence for the page, not a stack of pydantic internals."""
    if isinstance(exc, PydanticValidationError):
        first = exc.errors(include_url=False)[0]
        field = ".".join(str(p) for p in first["loc"]) or "input"
        return f"{field}: {first['msg']}"
    return str(exc)


def _parse_pairs(text: str) -> dict[str, str]:
    """"date = March 3rd, message = Your hearing moved" -> a context dict. Deliberately
    forgiving: this is typed by a person, not a machine."""
    pairs: dict[str, str] = {}
    for chunk in (text or "").split(","):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            if key.strip():
                pairs[key.strip()] = value.strip()
    return pairs


def _spec_from_form(form, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Overlay what the form asks onto whatever the type already had.

    The chase rhythm - how long to wait for a reply, how many follow-ups, how far
    apart - is not on the form; it comes from the template's defaults. Merging rather
    than replacing means editing a type in the UI cannot silently drop settings the
    form does not show, such as an uneven ladder like 1d, 3d.
    """
    keywords = [k.strip() for k in (form.get("escalate_keywords") or "").split(",") if k.strip()]
    spec: dict[str, Any] = {
        **(base or {}),
        "message": form.get("message") or "",
        "channel": form.get("channel") or "sms",
        "on_reply": form.get("on_reply") or "complete",
        "contact_role": form.get("contact_role") or "client",
        "escalate_keywords": keywords,
    }
    if spec["on_reply"] == "repeat":
        spec["repeat_every_days"] = int(form.get("repeat_every_days") or 14)
    else:
        spec.pop("repeat_every_days", None)
    return spec


@app.get("/workflows", response_class=HTMLResponse)
async def workflows_page(request: Request, edit: str | None = None,
                         s: AsyncSession = Depends(session), runtime: Runtime = Depends(rt)):
    types = await api_workflow_types(s, runtime)
    editing = next((t for t in types if t["name"] == edit and t["editable"]), None)
    return TEMPLATES.TemplateResponse(
        request, "workflows.html",
        {"types": types, "editing": editing, "spec": (editing or {}).get("spec") or {},
         "clock": clock_state(runtime), "error": request.query_params.get("error")},
    )


@app.post("/workflows")
async def workflows_create(request: Request, runtime: Runtime = Depends(rt)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    async with runtime.session_factory() as s:
        existing = {r.name: r for r in await list_types(s)}.get(name)
    try:  # a form must never 500 on what someone typed
        body = WorkflowTypeIn(
            name=name, template="outreach",
            description=(form.get("description") or "").strip() or None,
            spec=_spec_from_form(form, existing.spec if existing else None),
        )
    except (PydanticValidationError, ValueError) as exc:
        return RedirectResponse(f"/workflows?error={_form_error(exc)}", status_code=303)
    try:
        await api_create_workflow_type(body, runtime)
    except HTTPException as exc:
        return RedirectResponse(f"/workflows?error={exc.detail}", status_code=303)
    return RedirectResponse("/workflows", status_code=303)


@app.get("/instances/new", response_class=HTMLResponse)
async def new_instance_page(request: Request, workflow_type: str | None = None,
                            s: AsyncSession = Depends(session), runtime: Runtime = Depends(rt)):
    return TEMPLATES.TemplateResponse(
        request, "new_instance.html",
        {"types": await api_workflow_types(s, runtime), "selected": workflow_type,
         "clock": clock_state(runtime), "error": request.query_params.get("error")},
    )


@app.post("/instances/new")
async def new_instance_create(request: Request, runtime: Runtime = Depends(rt)):
    form = await request.form()
    try:
        body = NewAgentIn(
            workflow_type=form.get("workflow_type") or "",
            contact_name=(form.get("contact_name") or "").strip(),
            phone=(form.get("phone") or "").strip() or None,
            email=(form.get("email") or "").strip() or None,
            timezone=form.get("timezone") or "America/New_York",
            business_start=form.get("business_start") or "09:00",
            business_end=form.get("business_end") or "17:00",
            matter_type=(form.get("matter_type") or "").strip() or None,
            context=_parse_pairs(form.get("context_pairs") or ""),
        )
    except (PydanticValidationError, ValueError) as exc:
        return RedirectResponse(f"/instances/new?error={_form_error(exc)}", status_code=303)
    try:
        created = await api_start_agent(body, runtime)
    except HTTPException as exc:
        return RedirectResponse(f"/instances/new?error={exc.detail}", status_code=303)
    return RedirectResponse(f"/instances/{created['id']}", status_code=303)


@app.get("/instances/{instance_id}", response_class=HTMLResponse)
async def instance_page(
    instance_id: uuid.UUID, request: Request, s: AsyncSession = Depends(session), runtime: Runtime = Depends(rt)
):
    inst = await s.get(WorkflowInstance, instance_id)
    if inst is None:
        raise HTTPException(404, "instance not found")
    events = await list_events(s, instance_id)
    reviews = list(
        (await s.execute(select(ReviewTask).where(ReviewTask.instance_id == instance_id).order_by(ReviewTask.created_at))).scalars().all()
    )
    case = await s.get(CaseRecord, inst.case_id) if inst.case_id else None
    contacts = {}
    for key in ("client_contact_id", "provider_contact_id", "target_contact_id"):
        cid = inst.context.get(key)
        if cid:
            contacts[key] = await s.get(Contact, uuid.UUID(cid))
    return TEMPLATES.TemplateResponse(
        request, "instance.html",
        {"instance": inst, "events": events, "rounds": build_rounds(events),
         "reviews": reviews, "case": case, "contacts": contacts, "clock": clock_state(runtime),
         "ladders": ladder_sizes(runtime), "options": await resolution_options(runtime, s, reviews),
         "outcomes": list(runtime.registry.get(inst.workflow_type).outcomes_for(inst))},
    )


@app.post("/instances/{instance_id}/outcome")
async def record_outcome_form(instance_id: uuid.UUID, request: Request, runtime: Runtime = Depends(rt)):
    """"It is done" as a first-class action, not something you can only say when the
    agent happens to have asked. Records which ending it was, so the timeline says
    `records_received` rather than a bare close."""
    form = await request.form()
    action = (form.get("action") or "").strip()
    if not action:
        raise HTTPException(400, "an outcome needs an action")
    async with runtime.session_factory() as s, s.begin():
        try:
            await runtime.engine.record_outcome(
                s, instance_id, action, resolved_by=form.get("resolved_by") or "dashboard"
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return RedirectResponse(f"/instances/{instance_id}", status_code=303)


@app.post("/advance")
async def advance_form(request: Request, runtime: Runtime = Depends(rt)):
    form = await request.form()
    body = AdvanceIn(duration=(form.get("duration") or None))
    await api_advance(body, runtime)
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.post("/instances/{instance_id}/inbound")
async def inbound_form(instance_id: uuid.UUID, request: Request, runtime: Runtime = Depends(rt)):
    """Reply as the other party, from the instance page - the same path a real inbound
    webhook would take."""
    form = await request.form()
    text_ = (form.get("text") or "").strip()
    if text_:
        async with runtime.session_factory() as s, s.begin():
            try:
                await runtime.engine.handle_inbound(s, instance_id, text_, channel=form.get("channel") or "sms")
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from None
    return RedirectResponse(f"/instances/{instance_id}", status_code=303)


@app.post("/review-queue/{task_id}/resolve")
async def resolve_form(task_id: uuid.UUID, request: Request, runtime: Runtime = Depends(rt)):
    """Dashboard resolve buttons. `reference` is what a paralegal types after paying a
    fee or submitting a portal form - it rides along on the instance and gets cited in
    the next message to the other party."""
    form = await request.form()
    # No default. "close" completes the whole instance, and falling back to it when a
    # form arrives without an action would end a two-week chase on a dropped field.
    action = (form.get("action") or "").strip()
    if not action:
        raise HTTPException(400, "a resolution needs an action")
    resolution: dict[str, Any] = {"action": action}
    reference = (form.get("reference") or "").strip()
    if reference:
        resolution["reference"] = reference
    channel = (form.get("channel") or "").strip()
    if channel:
        resolution["channel"] = channel
    async with runtime.session_factory() as s, s.begin():
        try:
            await runtime.engine.resolve_review_task(
                s, task_id, resolution, resolved_by=form.get("resolved_by") or "dashboard"
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
