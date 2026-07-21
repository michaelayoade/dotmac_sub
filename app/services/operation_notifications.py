"""Best-effort real-time projection of durable network operation status."""

from __future__ import annotations

import logging
from typing import Literal

from app.services.realtime_platform import operation_topic, publish_topic_event

logger = logging.getLogger(__name__)


def publish_operation_status(
    operation_id: str,
    status: Literal["running", "succeeded", "failed", "warning"],
    message: str,
    *,
    operation_type: str = "ont_authorize",
    target_id: str | None = None,
    target_name: str | None = None,
    duration_ms: int | None = None,
    extra: dict | None = None,
) -> bool:
    """Publish a refetch hint without becoming operation-status authority."""
    try:
        published = publish_topic_event(
            operation_topic(operation_id),
            event_type="operation_status",
            payload={
                "operation_id": operation_id,
                "operation_type": operation_type,
                "status": status,
                "message": message,
                "target_id": target_id,
                "target_name": target_name,
                "duration_ms": duration_ms,
                **(extra or {}),
            },
        )
        if published:
            logger.info(
                "operation_notification_published",
                extra={
                    "event": "operation_notification",
                    "operation_id": operation_id,
                    "status": status,
                    "operation_type": operation_type,
                },
            )
        return published
    except Exception as exc:
        logger.warning(
            "operation_notification_failed operation_id=%s error=%s",
            operation_id,
            exc,
        )
        return False
