"""Thin read-mostly API + server-rendered dashboard. Deliberately generic: every
endpoint works for any workflow_type - nothing here knows a workflow by name."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CaseRecord, Contact, EntityFactVersion, ReviewTask, WorkflowInstance
from app.events import list_events
from app.review import list_pending_review_tasks
from app.runtime import Runtime, build_runtime, ensure_schema, get_runtime
from app.config import get_settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await ensure_schema(settings)
    rt = await build_runtime(settings)
    try:
        yield
    finally:
        await rt.close()


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


@app.get("/api/review-queue")
async def api_review_queue(s: AsyncSession = Depends(session)):
    tasks = await list_pending_review_tasks(s)
    return [
        {
            "id": str(t.id),
            "instance_id": str(t.instance_id),
            "reason": t.reason,
            "suggested_options": t.suggested_options,
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
        {"instances": instances, "reviews": reviews, "workflow_types": runtime.registry.types()},
    )


@app.get("/instances/{instance_id}", response_class=HTMLResponse)
async def instance_page(instance_id: uuid.UUID, request: Request, s: AsyncSession = Depends(session)):
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
        {"instance": inst, "events": events, "reviews": reviews, "case": case, "contacts": contacts},
    )


@app.post("/review-queue/{task_id}/resolve")
async def resolve_form(task_id: uuid.UUID, request: Request, runtime: Runtime = Depends(rt)):
    form = await request.form()
    async with runtime.session_factory() as s, s.begin():
        await runtime.engine.resolve_review_task(
            s, task_id, {"action": form.get("action", "close")}, resolved_by=form.get("resolved_by") or "dashboard"
        )
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
