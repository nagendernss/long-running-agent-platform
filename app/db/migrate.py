"""Plain-SQL migrations: apply migrations/*.sql in order, tracked in schema_migrations."""
from __future__ import annotations

import re
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _asyncpg_dsn(database_url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", database_url)


async def apply_migrations(database_url: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    conn = await asyncpg.connect(_asyncpg_dsn(database_url))
    applied: list[str] = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
    finally:
        await conn.close()
    return applied
