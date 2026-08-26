"""Channel adapter boundary. Real telephony/email/SMS integrations plug in here.

A Channel delivers one outbound message to an address (phone or email) and returns
the provider's external id. Address resolution (which phone number is *current*)
is NOT the channel's job - the Engine resolves it via the fact store and passes it in.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

# channel name -> which contact field holds the address
CHANNEL_ADDRESS_FIELD: dict[str, str] = {
    "call": "phone",
    "sms": "phone",
    "email": "email",
}


@dataclass(frozen=True)
class OutboundMessage:
    instance_id: uuid.UUID
    contact_id: uuid.UUID
    channel: str  # call | sms | email
    address: str
    body: str


class Channel(Protocol):
    name: str

    async def send(self, session: AsyncSession, message: OutboundMessage) -> str:
        """Deliver the message; return the provider-side external id."""
        ...
