"""Celery adapter for ZeptoMail delivery-state reconciliation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from app.celery_app import celery_app
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services import zeptomail_delivery_reconciliation as reconciliation
from app.services import zeptomail_delivery_transport as transport
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.zeptomail_delivery.reconcile_submitted_email")
def reconcile_submitted_email() -> dict[str, int]:
    session = db_session_adapter.create_session()
    stats = {"checked": 0, "updated": 0, "pending": 0, "errors": 0}
    try:
        if not transport.delivery_tracking_enabled(session):
            db_session_adapter.release_read_transaction(session)
            return stats
        client = transport.ZeptoMailLogClient.from_settings(session)
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        notification_ids = tuple(
            row[0]
            for row in (
                session.query(Notification.id)
                .filter(Notification.channel == NotificationChannel.email)
                .filter(Notification.status == NotificationStatus.submitted)
                .filter(Notification.created_at >= cutoff)
                .order_by(Notification.created_at.desc())
                .limit(50)
                .all()
            )
        )
        db_session_adapter.release_read_transaction(session)
        for notification_id in notification_ids:
            stats["checked"] += 1
            try:
                fact = client.lookup(notification_id)
            except (httpx.HTTPError, transport.ZeptoMailDeliveryConfigurationError):
                stats["errors"] += 1
                logger.warning(
                    "zeptomail_delivery_status_lookup_failed",
                    extra={"notification_id": str(notification_id)},
                    exc_info=True,
                )
                continue
            if fact is None:
                stats["pending"] += 1
                continue
            outcome = reconciliation.apply_zeptomail_delivery_status(
                session,
                reconciliation.ApplyZeptoMailDeliveryStatusCommand(
                    context=CommandContext.system(
                        actor="service:zeptomail-delivery-reconciler",
                        scope="notifications:delivery-reconciliation",
                        reason="Reconcile submitted email with ZeptoMail status",
                        idempotency_key=(
                            f"zeptomail:{fact.request_id or fact.email_reference or notification_id}:"
                            f"{fact.provider_status}"
                        ),
                    ),
                    notification_id=fact.notification_id,
                    provider_status=fact.provider_status,
                    observed_at=fact.observed_at,
                    email_reference=fact.email_reference,
                    request_id=fact.request_id,
                    reason=fact.reason,
                ),
            )
            stats["updated"] += int(outcome.kind == "updated")
            stats["pending"] += int(outcome.status is NotificationStatus.submitted)
        return stats
    finally:
        session.close()
