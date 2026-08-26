"""Composition root: wires clock, DB, scheduler, brain, channel, registry, engine."""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass

import asyncpg
import procrastinate
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent_brain import AgentBrain, RuleBasedAgentBrain
from app.channels import Channel, MockChannel
from app.clock import Clock, SystemClock
from app.config import Settings, get_settings
from app.db.migrate import apply_migrations
from app.db.session import make_engine, make_session_factory
from app.engine import Engine
from app.field_registry import FieldRegistry
from app.scheduling.scheduler import Scheduler, make_procrastinate_app
from app.workflows.registry import WorkflowRegistry, default_registry
from app.write_back import WriteBackResolver


def build_brain(settings: Settings, registry: WorkflowRegistry, field_registry: FieldRegistry) -> AgentBrain:
    """Pick the Agent Brain from config. Both implementations get their workflow
    knowledge injected from the registry, so neither imports a workflow module."""
    rules = RuleBasedAgentBrain(domain_rules_for=lambda wt: registry.get(wt).keyword_rules)
    if settings.agent_brain != "gemini":
        return rules
    if not settings.gemini_api_key:
        raise RuntimeError("AGENT_BRAIN=gemini but GEMINI_API_KEY is not set")
    from app.llm_brain import GeminiAgentBrain  # imported lazily: httpx only needed for this path

    return GeminiAgentBrain(
        settings.gemini_api_key,
        model=settings.gemini_model,
        domain_signals_for=lambda wt: registry.get(wt).domain_signals,
        field_registry=field_registry,
        fallback=rules,
    )


@dataclass
class Runtime:
    settings: Settings
    clock: Clock
    db_engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    procrastinate_app: procrastinate.App | None
    scheduler: Scheduler
    channel: Channel
    registry: WorkflowRegistry
    field_registry: FieldRegistry
    engine: Engine

    async def close(self) -> None:
        if self.procrastinate_app is not None:
            await self.procrastinate_app.close_async()
        await self.db_engine.dispose()
        global _runtime
        if _runtime is self:
            _runtime = None


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not built - call build_runtime() first")
    return _runtime


def configure_event_loop() -> None:
    """psycopg's async pool needs the selector loop on Windows."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def ensure_schema(settings: Settings) -> None:
    """Apply app migrations + Procrastinate's schema (once)."""
    await apply_migrations(settings.database_url)
    conn = await asyncpg.connect(re.sub(r"^postgresql\+\w+://", "postgresql://", settings.database_url))
    try:
        has_pq = await conn.fetchval("SELECT to_regclass('public.procrastinate_jobs') IS NOT NULL")
    finally:
        await conn.close()
    if not has_pq:
        app = make_procrastinate_app(settings.psycopg_url)
        async with app.open_async():
            await app.schema_manager.apply_schema_async()


async def build_runtime(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    channel: Channel | None = None,
    registry: WorkflowRegistry | None = None,
    durable: bool = True,
) -> Runtime:
    """durable=False skips Procrastinate entirely (instance.next_wake_at + poller only)."""
    global _runtime
    settings = settings or get_settings()
    clock = clock or SystemClock()
    channel = channel or MockChannel()
    registry = registry or default_registry()

    db_engine = make_engine(settings.sqlalchemy_url)
    session_factory = make_session_factory(db_engine)

    pq_app = None
    if durable:
        pq_app = make_procrastinate_app(settings.psycopg_url)
        await pq_app.open_async()
    scheduler = Scheduler(clock, pq_app)
    scheduler.durable = pq_app is not None

    field_registry = FieldRegistry.load(settings.field_registry_path)
    brain = build_brain(settings, registry, field_registry)
    engine = Engine(
        session_factory=session_factory,
        clock=clock,
        scheduler=scheduler,
        channel=channel,
        brain=brain,
        registry=registry,
        write_back=WriteBackResolver(field_registry),
    )
    await registry.reload_from(session_factory)   # pick up template-defined types
    _runtime = Runtime(
        settings=settings,
        clock=clock,
        db_engine=db_engine,
        session_factory=session_factory,
        procrastinate_app=pq_app,
        scheduler=scheduler,
        channel=channel,
        registry=registry,
        field_registry=field_registry,
        engine=engine,
    )
    return _runtime
