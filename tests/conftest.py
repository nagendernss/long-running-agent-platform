"""Test fixtures. Uses a real embedded Postgres (pgserver) unless TEST_DATABASE_URL is set."""
from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.channels import MockChannel
from app.clock import FakeClock
from app.config import Settings
from app.db.models import CaseRecord, Contact
from app.runtime import Runtime, build_runtime, configure_event_loop, ensure_schema

configure_event_loop()

ROOT = Path(__file__).resolve().parents[1]
TABLES = [
    "review_task", "entity_fact_version", "event", "workflow_instance", "case_record", "contact",
    "procrastinate_events", "procrastinate_jobs",
]
# workflow_type is NOT truncated - its seeded rows are the two workflows that used to
# be Python modules. Types a test creates are cleared instead, so they cannot leak.
SEEDED_TYPES = ("client_checkin", "contact_update")


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    import pgserver

    srv = pgserver.get_server(Path(tempfile.gettempdir()) / "hellocounsel_test_pg")
    srv.psql("DROP DATABASE IF EXISTS hc_test")
    srv.psql("CREATE DATABASE hc_test")
    try:
        yield srv.get_uri("hc_test")
    finally:
        srv.cleanup()


@pytest.fixture(scope="session")
def settings(database_url) -> Settings:
    return Settings(database_url=database_url, field_registry_path=str(ROOT / "config" / "field_registry.yaml"))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def schema(settings):
    await ensure_schema(settings)


@pytest_asyncio.fixture(loop_scope="session")
async def rt(settings, schema) -> Runtime:
    runtime = await build_runtime(settings, clock=FakeClock(), channel=MockChannel(), durable=True)
    async with runtime.db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE " + ", ".join(TABLES) + " CASCADE"))
        await conn.execute(
            text("DELETE FROM workflow_type WHERE name <> ALL(:keep)"), {"keep": list(SEEDED_TYPES)}
        )
    await runtime.registry.reload_from(runtime.session_factory)
    try:
        yield runtime
    finally:
        await runtime.close()


@dataclass
class Seed:
    client_id: uuid.UUID
    provider_id: uuid.UUID
    case_id: uuid.UUID
    client_phone: str = "+15550001"
    provider_phone: str = "+15550100"


@pytest_asyncio.fixture(loop_scope="session")
async def seed(rt: Runtime) -> Seed:
    now = rt.clock.now()
    async with rt.session_factory() as s, s.begin():
        client = Contact(id=uuid.uuid4(), name="Jane Client", role="client", phone="+15550001", email="jane@example.com", created_at=now)
        provider = Contact(
            id=uuid.uuid4(), name="Mercy Hospital Records", role="provider", phone="+15550100", email="records@mercy.example",
            timezone="America/Chicago", business_hours={"start": "08:00", "end": "16:00"}, created_at=now,
        )
        s.add_all([client, provider])
        await s.flush()
        case = CaseRecord(id=uuid.uuid4(), client_contact_id=client.id, matter_type="personal_injury", created_at=now)
        s.add(case)
        await s.flush()
        return Seed(client_id=client.id, provider_id=provider.id, case_id=case.id)
