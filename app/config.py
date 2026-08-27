from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    field_registry_path: str = "config/field_registry.yaml"
    agent_brain: str = "rules"          # rules | gemini
    # More than one key is allowed: the brain rotates to the next when one is out of
    # quota, which is the failure we actually hit rather than a hypothetical one.
    gemini_api_keys: tuple[str, ...] = ()
    gemini_model: str = "gemini-3.6-flash"

    @property
    def gemini_api_key(self) -> str | None:
        return self.gemini_api_keys[0] if self.gemini_api_keys else None

    @property
    def sqlalchemy_url(self) -> str:
        return _with_driver(self.database_url, "postgresql+asyncpg")

    @property
    def psycopg_url(self) -> str:
        return _with_driver(self.database_url, "postgresql")


def _with_driver(url: str, scheme: str) -> str:
    _, _, rest = url.partition("://")
    return f"{scheme}://{rest}"


def _gemini_keys() -> tuple[str, ...]:
    """GEMINI_API_KEY may hold several comma-separated keys; GEMINI_API_KEY_FALLBACK
    adds more. Order is priority order, and duplicates are dropped."""
    raw = [os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GEMINI_API_KEY_FALLBACK", "")]
    seen: list[str] = []
    for chunk in raw:
        for key in chunk.split(","):
            key = key.strip()
            if key and key not in seen:
                seen.append(key)
    return tuple(seen)


def get_settings(database_url: str | None = None) -> Settings:
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (see .env.example)")
    keys = _gemini_keys()
    # A key on its own is enough to opt in; AGENT_BRAIN=rules forces the baseline back on.
    brain = os.environ.get("AGENT_BRAIN") or ("gemini" if keys else "rules")
    return Settings(
        database_url=url,
        field_registry_path=os.environ.get("FIELD_REGISTRY_PATH", "config/field_registry.yaml"),
        agent_brain=brain,
        gemini_api_keys=keys,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
    )
