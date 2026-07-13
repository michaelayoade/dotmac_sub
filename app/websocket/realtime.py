"""Topic-level publish helpers over the existing WebSocket transport.

``ConnectionManager`` routes on an opaque key: team inbox uses conversation ids,
the workqueue uses ``workqueue:*`` channels. This module is the sync→async
bridge services call (they run in a thread, the manager is async), generalising
the pattern ``team_inbox_realtime`` established for conversations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.websocket.events import EventType, WebSocketEvent
from app.websocket.manager import get_connection_manager

logger = logging.getLogger(__name__)


async def broadcast_topic_event(
    topic: str,
    *,
    event_type: EventType,
    payload: dict[str, Any],
) -> None:
    manager = get_connection_manager()
    await manager.broadcast_to_topic(
        topic,
        WebSocketEvent(event=event_type, data=payload),
    )


def publish_topic_event(
    topic: str,
    *,
    event_type: EventType,
    payload: dict[str, Any],
) -> None:
    """Fire-and-forget publish callable from sync service code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(
                broadcast_topic_event(topic, event_type=event_type, payload=payload)
            )
        except Exception:
            logger.debug("topic_publish_failed topic=%s", topic, exc_info=True)
        return

    loop.create_task(
        broadcast_topic_event(topic, event_type=event_type, payload=payload)
    )
