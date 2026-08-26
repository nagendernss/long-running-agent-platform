"""Field Registry: which entity fields may be written back, and at what confidence."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class FieldPolicy:
    entity_type: str
    field: str
    auto_apply_threshold: float


class FieldRegistry:
    def __init__(self, policies: dict[tuple[str, str], FieldPolicy]):
        self._policies = policies

    @classmethod
    def load(cls, path: str | Path) -> "FieldRegistry":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        policies: dict[tuple[str, str], FieldPolicy] = {}
        for entity_type, fields in raw.items():
            for field, cfg in (fields or {}).items():
                policies[(entity_type, field)] = FieldPolicy(
                    entity_type=entity_type,
                    field=field,
                    auto_apply_threshold=float((cfg or {}).get("auto_apply_threshold", 1.0)),
                )
        return cls(policies)

    @classmethod
    def from_dict(cls, raw: dict) -> "FieldRegistry":
        policies = {
            (et, f): FieldPolicy(et, f, float(cfg.get("auto_apply_threshold", 1.0)))
            for et, fields in raw.items()
            for f, cfg in fields.items()
        }
        return cls(policies)

    def get(self, entity_type: str, field: str) -> FieldPolicy | None:
        return self._policies.get((entity_type, field))

    def is_writable(self, entity_type: str, field: str) -> bool:
        return (entity_type, field) in self._policies

    def field_names(self) -> set[str]:
        """Every writable field name - used to constrain what the Agent Brain may propose."""
        return {field for _entity_type, field in self._policies}
