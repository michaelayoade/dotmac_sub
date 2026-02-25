"""Helpers for queuing notifications when network backup jobs fail."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services import settings_spec

logger = logging.getLogger(__name__)


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _resolve_channel(value: object | None) -> NotificationChannel:
    if isinstance(value, NotificationChannel):
        return value
    if isinstance(value, str):
        try:
            return NotificationChannel(value.strip().lower())
        except ValueError:
            return NotificationChannel.email
    return NotificationChannel.email


def queue_backup_failure_notification(
    db: Session,
    *,
    device_kind: str,
    device_name: str,
    device_ip: str | None,
    error_message: str,
    run_type: str = "scheduled",
) -> bool:
    """Queue a notification for a backup failure using notification settings.

    Uses:
    - notification.alert_notifications_enabled
    - notification.alert_notifications_default_channel
    - notification.alert_notifications_default_recipient
    """
    enabled_raw = settings_spec.resolve_value(
        db, SettingDomain.notification, "alert_notifications_enabled"
    )
    enabled = _coerce_bool(
        enabled_raw if enabled_raw is not None else os.getenv("ALERT_NOTIFICATIONS_ENABLED", "true"),
        True,
    )
    if not enabled:
        return False

    recipient_raw = settings_spec.resolve_value(
        db, SettingDomain.notification, "alert_notifications_default_recipient"
    )
    recipient = str(
        recipient_raw if recipient_raw is not None else os.getenv("ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT", "")
    ).strip()
    if not recipient:
        return False

    channel_raw = settings_spec.resolve_value(
        db, SettingDomain.notification, "alert_notifications_default_channel"
    )
    if channel_raw is None:
        channel_raw = os.getenv("ALERT_NOTIFICATIONS_DEFAULT_CHANNEL", "email")
    channel = _resolve_channel(channel_raw)

    now = datetime.now(UTC)
    subject = f"[Backup Failure] {device_kind.upper()} {device_name}"
    body = (
        f"A {run_type} configuration backup failed.\n\n"
        f"Device type: {device_kind}\n"
        f"Device name: {device_name}\n"
        f"Management IP: {device_ip or '-'}\n"
        f"Timestamp (UTC): {now.isoformat()}\n"
        f"Error: {error_message}\n"
    )

    notification = Notification(
        channel=channel,
        recipient=recipient,
        subject=subject,
        body=body,
        status=NotificationStatus.queued,
    )
    db.add(notification)
    return True
