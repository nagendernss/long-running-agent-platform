"""Procrastinate worker: executes scheduled wake-ups durably.

    python scripts/worker.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.runtime import build_runtime, configure_event_loop, ensure_schema  # noqa: E402
from app.scheduling.recovery import recover  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    await ensure_schema(settings)
    rt = await build_runtime(settings)
    try:
        # Sweep once on start-up: after a crash or a deploy, anything that lost its
        # timer should be picked up now rather than at the next cron tick.
        logging.info("startup recovery: %s", (await recover(rt)).summary())
        await rt.procrastinate_app.run_worker_async(concurrency=4)
    finally:
        await rt.close()


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
