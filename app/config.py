from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    field_registry_path: str = "config/field_registry.yaml"

    @property
    def sqlalchemy_url(self) -> str:
        return _with_driver(self.database_url, "postgresql+asyncpg")

    @property
    def psycopg_url(self) -> str:
        return _with_driver(self.database_url, "postgresql")


def _with_driver(url: str, scheme: str) -> str:
    _, _, rest = url.partition("://")
    return f"{scheme}://{rest}"


def get_settings(database_url: str | None = None) -> Settings:
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (see .env.example)")
    return Settings(
        database_url=url,
        field_registry_path=os.environ.get("FIELD_REGISTRY_PATH", "config/field_registry.yaml"),
    )
