"""Publish network operation status notifications via WebSocket.

This module provides a synchronous interface for Celery tasks to publish
real-time operation status updates to connected WebSocket clients.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CHANNEL_PREFIX = "inbox_ws:"


def publish_operation_status(
    operation_id: str,
    status: Literal["running", "succeeded", "failed"],
    message: str,
    *,
    operation_type: str = "ont_authorize",
    target_id: str | None = None,
    target_name: str | None = None,
    duration_ms: int | None = None,
    extra: dict | None = None,
) -> bool:
    """Publish operation status update to WebSocket subscribers.

    Celery tasks call this after completing an operation to notify
    the UI in real-time.

    Args:
        operation_id: The NetworkOperation UUID (used as conversation_id).
        status: Current operation status.
        message: Human-readable status message.
        operation_type: Type of operation (e.g., "ont_authorize").
        target_id: ID of the target resource (e.g., OLT ID).
        target_name: Display name of the target resource.
        duration_ms: Operation duration in milliseconds.
        extra: Additional data to include in the notification.

    Returns:
        True if notification was published, False on error.
    """
    try:
        import redis

        event_data = {
            "event": "operation_status",
            "data": {
                "operation_id": operation_id,
                "operation_type": operation_type,
                "status": status,
                "message": message,
                "target_id": target_id,
                "target_name": target_name,
                "duration_ms": duration_ms,
                **(extra or {}),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        payload = json.dumps({
            "conversation_id": operation_id,
            "event": event_data,
        })

        client = redis.from_url(REDIS_URL, decode_responses=True)
        try:
            client.publish(f"{CHANNEL_PREFIX}{operation_id}", payload)
            logger.info(
                "operation_notification_published",
                extra={
                    "event": "operation_notification",
                    "operation_id": operation_id,
                    "status": status,
                    "operation_type": operation_type,
                },
            )
            return True
        finally:
            client.close()
    except Exception as exc:
        logger.warning(
            "operation_notification_failed operation_id=%s error=%s",
            operation_id,
            exc,
        )
        return False
