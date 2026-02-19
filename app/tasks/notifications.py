from datetime import UTC, datetime, timedelta

from sqlalchemy import or_

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services import email as email_service

# Timeout for stuck "sending" notifications (5 minutes)
SENDING_TIMEOUT_MINUTES = 5


def _deliver_notification_queue(db, batch_size: int = 50) -> int:
    now = datetime.now(UTC)
    stuck_threshold = now - timedelta(minutes=SENDING_TIMEOUT_MINUTES)

    # Query both queued notifications and stuck "sending" notifications
    notifications = (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(Notification.channel == NotificationChannel.email)
        .filter(
            or_(
                # Queued notifications ready to send
                Notification.status == NotificationStatus.queued,
                # Stuck "sending" notifications (likely crashed during send)
                # Use updated_at to detect stuck notifications
                (
                    (Notification.status == NotificationStatus.sending)
                    & (Notification.updated_at < stuck_threshold)
                ),
            )
        )
        .filter(
            (Notification.send_at.is_(None))
            | (Notification.send_at <= now)
        )
        .order_by(Notification.created_at.asc())
        .limit(batch_size)
        .all()
    )
    delivered = 0
    for notification in notifications:
        # Update status before sending - updated_at auto-updates
        notification.status = NotificationStatus.sending
        db.commit()

        subject = notification.subject or "Notification"
        body = notification.body or ""
        try:
            success = email_service.send_email(
                db=db,
                to_email=notification.recipient,
                subject=subject,
                body_html=body,
                body_text=None,
                track=False,
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
            notification.status = NotificationStatus.failed
            if not notification.last_error:
                notification.last_error = "send_email_failed"
        db.commit()
    return delivered


@celery_app.task(name="app.tasks.notifications.deliver_notification_queue")
def deliver_notification_queue():
    session = SessionLocal()
    try:
        _deliver_notification_queue(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
