"""Shared queue helpers for staff/internal notifications."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel, NotificationStatus
from app.schemas.notification import NotificationCreate
from app.services.notification import notifications as notifications_svc


def queue_staff_notification(
    db: Session,
    *,
    channel: NotificationChannel,
    recipient: str,
    subject: str,
    body: str,
    delivered: bool = False,
    sent_at: datetime | None = None,
) -> None:
    """Queue an internal notification without customer preference/status policy."""
    if not recipient:
        return
    notifications_svc.queue_internal_notification(
        db,
        NotificationCreate(
            channel=channel,
            recipient=recipient,
            subject=subject,
            body=body,
            status=NotificationStatus.delivered
            if delivered
            else NotificationStatus.queued,
            sent_at=sent_at or (datetime.now(UTC) if delivered else None),
        ),
    )


def queue_staff_push(
    db: Session,
    *,
    recipient: str,
    subject: str,
    body: str,
    delivered: bool = True,
) -> None:
    queue_staff_notification(
        db,
        channel=NotificationChannel.push,
        recipient=recipient,
        subject=subject,
        body=body,
        delivered=delivered,
    )


def queue_staff_email(
    db: Session,
    *,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    queue_staff_notification(
        db,
        channel=NotificationChannel.email,
        recipient=recipient,
        subject=subject,
        body=body,
    )
