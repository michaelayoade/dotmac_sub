from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


class EventType(str, Enum):
    """WebSocket event types for the inbox."""

    MESSAGE_NEW = "message_new"
    MESSAGE_STATUS_CHANGED = "message_status_changed"
    CONVERSATION_UPDATED = "conversation_updated"
    CONVERSATION_SUMMARY = "conversation_summary"
    USER_TYPING = "user_typing"
    CONNECTION_ACK = "connection_ack"
    HEARTBEAT = "heartbeat"


class WebSocketEvent(BaseModel):
    """Outbound WebSocket event sent to clients."""

    event: EventType
    data: dict[str, Any]
    timestamp: datetime | None = None

    def model_post_init(self, __context: Any) -> None:
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
    conversation_id: str | None = None
    data: dict[str, Any] | None = None
