from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.services.realtime_platform import EventType


class WebSocketEvent(BaseModel):
    """Outbound WebSocket event sent to clients."""

    event: EventType
    data: dict[str, Any]
    timestamp: datetime | None = None

    def model_post_init(self, _context: Any) -> None:
        if self.timestamp is None:
            object.__setattr__(self, "timestamp", datetime.now(UTC))


class InboundMessageType(str, Enum):
    """Types of messages clients can send."""

    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    TYPING = "typing"
    PING = "ping"


class InboundMessage(BaseModel):
    """Message received from WebSocket client."""

    type: InboundMessageType
    topic: str | None = None
    conversation_id: str | None = None
    data: dict[str, Any] | None = None

    @property
    def requested_topic(self) -> str | None:
        """Explicit v1 topic, with the old conversation_id field as a shim."""
        return self.topic or self.conversation_id
