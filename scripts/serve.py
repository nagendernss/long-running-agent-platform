"""Run the API + dashboard.

    python scripts/serve.py [--host 0.0.0.0] [--port 8000]

Why not plain `uvicorn app.api.main:app`? On Windows uvicorn installs a
ProactorEventLoop, and psycopg (used by Procrastinate) refuses to run async on it.
This entrypoint owns the loop (`loop="none"` + SelectorEventLoop) so the same
command works on every platform. On Linux/macOS `uvicorn app.api.main:app` is fine.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from app.api.main import app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, loop="none", log_level="info"))
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
