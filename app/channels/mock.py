"""Mock channel: records every send in memory (for tests/demo). The Engine logs the
`message_sent` event itself, so any real channel gets the same audit trail for free."""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import OutboundMessage


class MockChannel:
    name = "mock"

    def __init__(self) -> None:
        self.outbox: list[OutboundMessage] = []

    async def send(self, session: AsyncSession, message: OutboundMessage) -> str:
        self.outbox.append(message)
        return f"mock-{uuid.uuid4().hex[:8]}"

    def sent_to(self, address: str) -> list[OutboundMessage]:
        return [m for m in self.outbox if m.address == address]
