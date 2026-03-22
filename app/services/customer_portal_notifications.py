"""Customer portal notification page helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.models.comms import CustomerNotificationEvent
from app.models.notification import Notification, NotificationStatus
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid


def _normalize_portal_notification(item: object) -> SimpleNamespace:
    if isinstance(item, CustomerNotificationEvent):
        return SimpleNamespace(
            channel=item.channel,
            created_at=item.created_at,
            entity_type=item.entity_type,
            message=item.message,
            status=SimpleNamespace(value=item.status.value),
        )

    notification = item
    template = getattr(notification, "template", None)
    status_value = getattr(getattr(notification, "status", None), "value", "pending")
    if status_value == NotificationStatus.delivered.value:
        status_value = "sent"
    elif status_value in {NotificationStatus.queued.value, NotificationStatus.sending.value}:
        status_value = "pending"
    return SimpleNamespace(
        channel=getattr(getattr(notification, "channel", None), "value", getattr(notification, "channel", "")),
        created_at=getattr(notification, "created_at", None),
        entity_type=getattr(template, "code", None) or "notification",
        message=getattr(notification, "body", None) or getattr(notification, "subject", "") or "",
        status=SimpleNamespace(value=status_value),
    )


def get_notifications_page(
    db: Session,
    customer: dict,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    subscriber_id = customer.get("subscriber_id") or customer.get("session", {}).get("subscriber_id")
    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id)) if subscriber_id else None

    recipients: list[str] = []
    if subscriber:
        if subscriber.email:
            recipients.append(subscriber.email)
        if subscriber.phone:
            recipients.append(subscriber.phone)

    notifications: list[SimpleNamespace] = []
    total = 0
    if recipients:
        queue_notifications = (
            db.query(Notification)
            .filter(Notification.recipient.in_(recipients))
            .filter(Notification.is_active.is_(True))
            .all()
        )
        customer_notifications = (
            db.query(CustomerNotificationEvent)
            .filter(CustomerNotificationEvent.recipient.in_(recipients))
            .all()
        )
        merged = [
            _normalize_portal_notification(item)
            for item in [*queue_notifications, *customer_notifications]
        ]
        merged.sort(
            key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        total = len(merged)
        offset = (page - 1) * per_page
        notifications = merged[offset : offset + per_page]

    return {
        "notifications": notifications,
        "page": page,
        "per_page": per_page,
        "total": total,
    }
