"""Run the API + dashboard.

    python scripts/serve.py                       # uses DATABASE_URL from .env
    python scripts/serve.py --embedded --seed             # embedded Postgres + a demo story
    python scripts/serve.py --embedded --reset --seed     # flush first, then replay the story

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--embedded", action="store_true", help="start a bundled Postgres instead of using DATABASE_URL")
    ap.add_argument("--seed", action="store_true", help="play a demo story in if the database is empty")
    ap.add_argument("--reset", action="store_true", help="delete every row first (the schema is kept)")
    ap.add_argument("--real-clock", action="store_true",
                    help="use the system clock; by default this dev server can time-travel")
    args = ap.parse_args()

    if args.embedded:
        os.environ["DATABASE_URL"] = start_embedded_postgres()
        print(f"embedded postgres: {os.environ['DATABASE_URL']}")
    os.environ.setdefault("FIELD_REGISTRY_PATH", str(ROOT / "config" / "field_registry.yaml"))

    import uvicorn

    from app.api.main import app
    from app.clock import OffsetClock
    from app.config import get_settings
    from app.runtime import build_runtime, ensure_schema
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_story import reset_database, seed_story

    loop = asyncio.SelectorEventLoop()  # psycopg cannot use the Windows Proactor loop
    asyncio.set_event_loop(loop)
    try:
        if args.seed or args.reset:
            loop.run_until_complete(ensure_schema(get_settings()))
        if args.reset:
            loop.run_until_complete(reset_database())
        if args.seed:
            loop.run_until_complete(seed_story())
        if not args.real_clock:
            # Build the runtime up front on a clock an operator can push forward, so a
            # 14-day wait can be watched now. app.api.main's lifespan reuses it.
            loop.run_until_complete(ensure_schema(get_settings()))
            loop.run_until_complete(build_runtime(get_settings(), clock=OffsetClock()))
            print("time travel enabled: use the header buttons or POST /api/simulate/advance")
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, loop="none", log_level="info"))
        print(f"dashboard: http://{args.host}:{args.port}/")
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
