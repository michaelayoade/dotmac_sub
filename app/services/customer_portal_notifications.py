"""Customer portal notification page helpers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.comms import CustomerNotificationEvent
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid


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

    notifications: list[CustomerNotificationEvent] = []
    total = 0
    if recipients:
        offset = (page - 1) * per_page
        query = (
            db.query(CustomerNotificationEvent)
            .filter(CustomerNotificationEvent.recipient.in_(recipients))
            .order_by(CustomerNotificationEvent.created_at.desc())
        )
        total = query.count()
        notifications = query.offset(offset).limit(per_page).all()

    return {
        "notifications": notifications,
        "page": page,
        "per_page": per_page,
        "total": total,
    }
