from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.websocket.events import EventType, WebSocketEvent
from app.websocket.manager import get_connection_manager

logger = logging.getLogger(__name__)


def message_event_payload(
    *,
    conversation_id: str,
    message_id: str,
    body: str | None,
    direction: str,
    channel_type: str,
    created_at: datetime | None,
    author_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "id": message_id,
        "body": body,
        "direction": direction,
        "channel_type": channel_type,
        "created_at": created_at.isoformat() if created_at else None,
    }
    if author_name:
        payload["author_name"] = author_name
    if extra:
        payload.update(extra)
    return payload


async def broadcast_conversation_event(
    conversation_id: str,
    *,
    event_type: EventType,
    payload: dict[str, Any],
) -> None:
    manager = get_connection_manager()
    await manager.broadcast_to_conversation(
        conversation_id,
        WebSocketEvent(event=event_type, data=payload),
    )


def publish_conversation_event(
    conversation_id: str,
    *,
    event_type: EventType,
    payload: dict[str, Any],
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(
                broadcast_conversation_event(
                    conversation_id,
                    event_type=event_type,
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("team_inbox_realtime_publish_failed", exc_info=True)
        return

    loop.create_task(
        broadcast_conversation_event(
            conversation_id,
            event_type=event_type,
            payload=payload,
        )
    )
