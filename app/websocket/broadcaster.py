"""WebSocket event broadcaster - CRM functionality removed."""

from __future__ import annotations

from typing import Any

from app.logging import get_logger

logger = get_logger(__name__)


def broadcast_new_message(_message: Any, _conversation: Any):
    """Broadcast a new message event - no-op, CRM removed."""
    pass


def broadcast_message_status(_message_id: str, _conversation_id: str, _status: str):
    """Broadcast a message status change event - no-op, CRM removed."""
    pass


def broadcast_conversation_updated(_conversation: Any):
    """Broadcast a conversation update event - no-op, CRM removed."""
    pass


def broadcast_conversation_summary(_conversation_id: str, _summary: dict):
    """Broadcast a conversation summary update - no-op, CRM removed."""
    pass
