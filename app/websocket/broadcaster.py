from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.logging import get_logger
from app.websocket.events import EventType, WebSocketEvent
from app.websocket.manager import get_connection_manager

if TYPE_CHECKING:
    from app.models.crm.conversation import Conversation, Message

logger = get_logger(__name__)


def _run_async(coro):
    """Run an async coroutine from sync code."""
    try:
        loop = asyncio.get_running_loop()
        asyncio.create_task(coro)
    except RuntimeError:
        asyncio.run(coro)


def broadcast_new_message(message: "Message", conversation: "Conversation"):
    """
    Broadcast a new message event to conversation subscribers.

    Called from inbox.py after creating a new message.
    """
    try:
        event = WebSocketEvent(
            event=EventType.MESSAGE_NEW,
            data={
                "message_id": str(message.id),
                "conversation_id": str(conversation.id),
                "channel_type": message.channel_type.value if message.channel_type else None,
                "direction": message.direction.value if message.direction else None,
                "status": message.status.value if message.status else None,
                "body_preview": (message.body[:100] + "...") if message.body and len(message.body) > 100 else message.body,
                "subject": message.subject,
                "person_id": str(conversation.person_id) if conversation.person_id else None,
            },
        )
        manager = get_connection_manager()
        _run_async(manager.broadcast_to_conversation(str(conversation.id), event))
        logger.debug(
            "broadcast_new_message conversation_id=%s message_id=%s",
            conversation.id,
            message.id,
        )
    except Exception as exc:
        logger.warning("broadcast_new_message_error error=%s", exc)


def broadcast_message_status(
    message_id: str, conversation_id: str, status: str
):
    """
    Broadcast a message status change event.

    Called from inbox.py after updating message status.
    """
    try:
        event = WebSocketEvent(
            event=EventType.MESSAGE_STATUS_CHANGED,
            data={
                "message_id": str(message_id),
                "conversation_id": str(conversation_id),
                "status": status,
            },
        )
        manager = get_connection_manager()
        _run_async(manager.broadcast_to_conversation(str(conversation_id), event))
        logger.debug(
            "broadcast_message_status conversation_id=%s message_id=%s status=%s",
            conversation_id,
            message_id,
            status,
        )
    except Exception as exc:
        logger.warning("broadcast_message_status_error error=%s", exc)


def broadcast_conversation_updated(conversation: "Conversation"):
    """
    Broadcast a conversation update event.

    Called when conversation metadata changes (status, assignee, etc).
    """
    try:
        event = WebSocketEvent(
            event=EventType.CONVERSATION_UPDATED,
            data={
                "conversation_id": str(conversation.id),
                "status": conversation.status.value if conversation.status else None,
                "is_active": conversation.is_active,
                "person_id": str(conversation.person_id) if conversation.person_id else None,
            },
        )
        manager = get_connection_manager()
        _run_async(manager.broadcast_to_conversation(str(conversation.id), event))
        logger.debug(
            "broadcast_conversation_updated conversation_id=%s", conversation.id
        )
    except Exception as exc:
        logger.warning("broadcast_conversation_updated_error error=%s", exc)


def broadcast_conversation_summary(conversation_id: str, summary: dict):
    """Broadcast a lightweight conversation summary update."""
    try:
        payload = dict(summary)
        payload["conversation_id"] = str(conversation_id)
        event = WebSocketEvent(
            event=EventType.CONVERSATION_SUMMARY,
            data=payload,
        )
        manager = get_connection_manager()
        _run_async(manager.broadcast_to_conversation(str(conversation_id), event))
    except Exception as exc:
        logger.warning("broadcast_conversation_summary_error error=%s", exc)
