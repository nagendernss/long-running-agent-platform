"""Run the API + dashboard.

    python scripts/serve.py                       # uses DATABASE_URL from .env
    python scripts/serve.py --embedded --seed     # embedded Postgres + demo data, nothing to install

Why not plain `uvicorn app.api.main:app`? On Windows uvicorn installs a
ProactorEventLoop, and psycopg (used by Procrastinate) refuses to run async on it.
This entrypoint owns the loop (`loop="none"` + SelectorEventLoop) so the same
command works on every platform. On Linux/macOS `uvicorn app.api.main:app` is fine.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def start_embedded_postgres() -> str:
    """Boot a bundled Postgres under .pgdata and return its URL (no Docker needed)."""
    import pgserver

    srv = pgserver.get_server(ROOT / ".pgdata")
    if not srv.psql("SELECT 1 FROM pg_database WHERE datname='hellocounsel'").strip().endswith("(1 row)"):
        srv.psql("CREATE DATABASE hellocounsel")
    return srv.get_uri("hellocounsel")


async def seed_demo_data() -> None:
    """Create a case with live instances so the dashboard has something to show:
    one medical-records instance escalated to the review queue, one check-in waiting."""
    from sqlalchemy import select

    from app.config import get_settings
    from app.db.models import CaseRecord, Contact, WorkflowInstance
    from app.runtime import build_runtime

    rt = await build_runtime(get_settings())
    try:
        async with rt.session_factory() as s, s.begin():
            if (await s.execute(select(WorkflowInstance.id).limit(1))).scalar_one_or_none():
                return  # already seeded
            now = rt.clock.now()
            client = Contact(id=uuid.uuid4(), name="Jane Client", role="client", phone="+15550001", email="jane@example.com", created_at=now)
            provider = Contact(
                id=uuid.uuid4(), name="Mercy Hospital Records", role="provider", phone="+15550100",
                email="records@mercy.example", timezone="America/Chicago",
                business_hours={"start": "08:00", "end": "16:00"}, created_at=now,
            )
            s.add_all([client, provider])
            await s.flush()
            case = CaseRecord(id=uuid.uuid4(), client_contact_id=client.id, matter_type="personal_injury", created_at=now)
            s.add(case)
            await s.flush()
            medical = await rt.engine.start_instance(
                s, "medical_records_followup", case_id=case.id,
                context={
                    "target_contact_id": str(provider.id), "provider_contact_id": str(provider.id),
                    "client_contact_id": str(client.id), "provider_channel": "call", "client_channel": "sms",
                },
            )
            await rt.engine.start_instance(
                s, "client_checkin", case_id=case.id,
                context={"target_contact_id": str(client.id), "client_contact_id": str(client.id), "client_channel": "sms"},
            )
            mid = medical.id

        # a wrong-number correction, then no-answers until the retry ladder escalates
        async with rt.session_factory() as s, s.begin():
            await rt.engine.handle_inbound(s, mid, "wrong number, it's actually 555-0199", channel="call")
        for _ in range(4):
            async with rt.session_factory() as s, s.begin():
                await rt.engine.handle_inbound(s, mid, "no answer, voicemail", channel="call")
    finally:
        await rt.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--embedded", action="store_true", help="start a bundled Postgres instead of using DATABASE_URL")
    ap.add_argument("--seed", action="store_true", help="insert demo instances if the database is empty")
    args = ap.parse_args()

    if args.embedded:
        os.environ["DATABASE_URL"] = start_embedded_postgres()
        print(f"embedded postgres: {os.environ['DATABASE_URL']}")
    os.environ.setdefault("FIELD_REGISTRY_PATH", str(ROOT / "config" / "field_registry.yaml"))

    import uvicorn

    from app.api.main import app
    from app.config import get_settings
    from app.runtime import ensure_schema

    loop = asyncio.SelectorEventLoop()  # psycopg cannot use the Windows Proactor loop
    asyncio.set_event_loop(loop)
    try:
        if args.seed:
            loop.run_until_complete(ensure_schema(get_settings()))
            loop.run_until_complete(seed_demo_data())
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, loop="none", log_level="info"))
        print(f"dashboard: http://{args.host}:{args.port}/")
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
