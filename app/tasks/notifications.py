"""Celery tasks for notification delivery."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_

from app.celery_app import celery_app
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services import email as email_service
from app.services import sms as sms_service
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations.connectors import whatsapp as whatsapp_service

logger = logging.getLogger(__name__)

# Timeout for stuck "sending" notifications (5 minutes)
SENDING_TIMEOUT_MINUTES = 5
# Maximum delivery retries before marking as permanently failed
MAX_RETRIES = 3


def _deliver_notification_queue_stats(db, batch_size: int = 50) -> dict[str, int]:
    now = datetime.now(UTC)
    stuck_threshold = now - timedelta(minutes=SENDING_TIMEOUT_MINUTES)

    # Query queued, stuck "sending", and retryable failed notifications
    notifications = (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(
            Notification.channel.in_(
                [
                    NotificationChannel.email,
                    NotificationChannel.sms,
                    NotificationChannel.whatsapp,
                ]
            )
        )
        .filter(
            or_(
                # Queued notifications ready to send
                Notification.status == NotificationStatus.queued,
                # Stuck "sending" notifications (likely crashed during send)
                (
                    (Notification.status == NotificationStatus.sending)
                    & (Notification.updated_at < stuck_threshold)
                ),
                # Failed notifications eligible for retry (under max retries)
                (
                    (Notification.status == NotificationStatus.failed)
                    & (Notification.retry_count < MAX_RETRIES)
                ),
            )
        )
        .filter((Notification.send_at.is_(None)) | (Notification.send_at <= now))
        .order_by(Notification.created_at.asc())
        .limit(batch_size)
        .all()
    )
    delivered = 0
    retried = 0
    failed = 0
    for notification in notifications:
        # Update status before sending
        notification.status = NotificationStatus.sending
        db.commit()

        subject = notification.subject or "Notification"
        body = notification.body or ""
        try:
            if notification.channel == NotificationChannel.email:
                success = email_service.send_email(
                    db=db,
                    to_email=notification.recipient,
                    subject=subject,
                    body_html=body,
                    body_text=None,
                    track=False,
                    activity="notification_queue",
                )
            elif notification.channel == NotificationChannel.sms:
                success = sms_service.send_sms(
                    db=db,
                    to_phone=notification.recipient,
                    body=body,
                    track=False,
                )
            elif notification.channel == NotificationChannel.whatsapp:
                result = whatsapp_service.send_text_message(
                    db=db,
                    recipient=notification.recipient,
                    body=body,
                    dry_run=False,
                )
                success = bool(result.get("ok"))
                if not success:
                    notification.last_error = str(
                        result.get("response") or "whatsapp_send_failed"
                    )
            else:
                success = False
                notification.last_error = (
                    f"unsupported_channel:{notification.channel.value}"
                )
        except Exception as exc:
            success = False
            notification.last_error = str(exc)

        if success:
            notification.status = NotificationStatus.delivered
            notification.sent_at = datetime.now(UTC)
            notification.last_error = None
            delivered += 1
        else:
            notification.retry_count = (notification.retry_count or 0) + 1
            if notification.retry_count >= MAX_RETRIES:
                notification.status = NotificationStatus.failed
                failed += 1
                logger.warning(
                    "Notification %s permanently failed after %d retries: %s",
                    notification.id,
                    notification.retry_count,
                    notification.last_error,
                )
            else:
                # Schedule for retry — set back to failed, will be picked up next run
                notification.status = NotificationStatus.failed
                retried += 1
                logger.info(
                    "Notification %s retry %d/%d scheduled",
                    notification.id,
                    notification.retry_count,
                    MAX_RETRIES,
                )
            if not notification.last_error:
                notification.last_error = "send_failed"
        db.commit()

    return {"delivered": delivered, "retried": retried, "failed": failed}


def _deliver_notification_queue(db, batch_size: int = 50) -> int:
    return _deliver_notification_queue_stats(db, batch_size=batch_size)["delivered"]


@celery_app.task(name="app.tasks.notifications.deliver_notification_queue")
def deliver_notification_queue() -> dict[str, int]:
    """Process queued notifications and retry failed ones."""
    with db_session_adapter.session() as session:
        result = _deliver_notification_queue_stats(session)
        logger.info(
            "Notification queue processed: delivered=%d, retried=%d, failed=%d",
            result["delivered"],
            result["retried"],
            result["failed"],
        )
        return result
