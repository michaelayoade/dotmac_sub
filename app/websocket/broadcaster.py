"""WebSocket event broadcaster - CRM functionality removed."""

from __future__ import annotations

from typing import Any

from app.logging import get_logger

logger = get_logger(__name__)


def broadcast_new_message(message: Any, conversation: Any):
    """Broadcast a new message event - no-op, CRM removed."""
    pass


def broadcast_message_status(message_id: str, conversation_id: str, status: str):
    """Broadcast a message status change event - no-op, CRM removed."""
    pass


def broadcast_conversation_updated(conversation: Any):
    """Broadcast a conversation update event - no-op, CRM removed."""
    pass


def broadcast_conversation_summary(conversation_id: str, summary: dict):
    """Broadcast a conversation summary update - no-op, CRM removed."""
    pass
