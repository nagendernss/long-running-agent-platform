from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Signal(BaseModel):
    """Structured outcome extracted from a raw message/call by the Agent Brain."""

    type: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: Optional[str] = None  # snippet of source text that justified the signal

    def describe(self) -> str:
        data = self.model_dump(exclude={"type", "confidence", "evidence"}, exclude_none=True)
        return f"{self.type}({', '.join(f'{k}={v!r}' for k, v in data.items())})"
