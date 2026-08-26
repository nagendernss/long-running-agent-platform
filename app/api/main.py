"""Thin read-mostly API + server-rendered dashboard. Deliberately generic: every
endpoint works for any workflow_type - nothing here knows a workflow by name."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CaseRecord, Contact, EntityFactVersion, ReviewTask, WorkflowInstance
from app.api.timeline import build_rounds
from app.clock import OffsetClock, parse_duration
from app.events import list_events
from app.review import list_pending_review_tasks
from app.runtime import Runtime, build_runtime, ensure_schema, get_runtime
from app.config import get_settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
        options[str(task.id)] = list(getattr(runtime.registry.get(inst.workflow_type), "resolution_options", []))
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
         "ladders": ladder_sizes(runtime), "options": await resolution_options(runtime, s, reviews)},
    )


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
    resolution: dict[str, Any] = {"action": form.get("action") or "close"}
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
