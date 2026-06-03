"""Customer portal notification page helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.comms import CustomerNotificationEvent, CustomerNotificationStatus
from app.models.notification import Notification, NotificationStatus
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.customer_notification_policy import (
    get_subscriber_notification_preferences,
    is_notification_enabled_for_subscriber,
    resolve_notification_category,
)


def _normalize_portal_notification(item: object) -> SimpleNamespace:
    if isinstance(item, CustomerNotificationEvent):
        return SimpleNamespace(
            channel=item.channel,
            created_at=item.created_at,
            entity_type=item.entity_type,
            category=resolve_notification_category(item.entity_type),
            message=item.message,
            recipient=item.recipient,
            status=SimpleNamespace(value=item.status.value),
            subscriber_id=item.subscriber_id,
        )

    notification = item
    template = getattr(notification, "template", None)
    event_type = getattr(notification, "event_type", None) or getattr(
        template,
        "code",
        None,
    )
    return SimpleNamespace(
        channel=getattr(
            getattr(notification, "channel", None),
            "value",
            getattr(notification, "channel", ""),
        ),
        created_at=getattr(notification, "created_at", None),
        entity_type=event_type or "notification",
        category=getattr(notification, "category", None)
        or resolve_notification_category(event_type),
        message=getattr(notification, "body", None)
        or getattr(notification, "subject", "")
        or "",
        recipient=getattr(notification, "recipient", ""),
        status=SimpleNamespace(value="sent"),
        subscriber_id=getattr(notification, "subscriber_id", None),
    )


def _resolve_notification_context(
    db: Session,
    customer: dict,
) -> tuple[Subscriber | None, list[str]]:
    subscriber_id = customer.get("subscriber_id") or customer.get("session", {}).get(
        "subscriber_id"
    )
    subscriber = (
        db.get(Subscriber, coerce_uuid(subscriber_id)) if subscriber_id else None
    )

    recipients: list[str] = []
    if subscriber:
        if subscriber.email:
            recipients.append(subscriber.email)
        if subscriber.phone:
            recipients.append(subscriber.phone)

    return subscriber, recipients


def _is_visible_in_portal(
    db: Session,
    *,
    subscriber: Subscriber | None,
    item: SimpleNamespace,
) -> bool:
    if subscriber is None:
        return False
    return is_notification_enabled_for_subscriber(
        db,
        subscriber_id=subscriber.id,
        channel=item.channel,
        category=item.category,
        recipient=item.recipient,
    )


def _load_notifications(
    db: Session,
    *,
    subscriber: Subscriber | None,
    recipients: list[str],
) -> list[SimpleNamespace]:
    if subscriber is None and not recipients:
        return []

    notification_filters = []
    comms_filters = []
    if subscriber is not None:
        notification_filters.append(Notification.subscriber_id == subscriber.id)
        comms_filters.append(CustomerNotificationEvent.subscriber_id == subscriber.id)
    if recipients:
        notification_filters.append(
            and_(
                Notification.subscriber_id.is_(None),
                Notification.recipient.in_(recipients),
            )
        )
        comms_filters.append(
            and_(
                CustomerNotificationEvent.subscriber_id.is_(None),
                CustomerNotificationEvent.recipient.in_(recipients),
            )
        )

    queue_notifications = (
        db.query(Notification)
        .filter(Notification.is_active.is_(True))
        .filter(Notification.status == NotificationStatus.delivered)
        .filter(or_(*notification_filters))
        .all()
        if notification_filters
        else []
    )
    customer_notifications = (
        db.query(CustomerNotificationEvent)
        .filter(CustomerNotificationEvent.status == CustomerNotificationStatus.sent)
        .filter(or_(*comms_filters))
        .all()
        if comms_filters
        else []
    )

    merged = [
        normalized
        for normalized in (
            _normalize_portal_notification(item)
            for item in [*queue_notifications, *customer_notifications]
        )
        if _is_visible_in_portal(
            db,
            subscriber=subscriber,
            item=normalized,
        )
    ]
    merged.sort(
        key=lambda item: item.created_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return merged


def get_notifications_preview(
    db: Session,
    customer: dict,
    *,
    limit: int = 5,
) -> dict[str, object]:
    subscriber, recipients = _resolve_notification_context(db, customer)
    notifications = _load_notifications(
        db, subscriber=subscriber, recipients=recipients
    )
    total = len(notifications)
    return {
        "recent_notifications": notifications[:limit],
        "recent_notifications_total": total,
        "unread_notifications_count": total,
        "has_recent_notifications": total > 0,
    }


def get_notifications_page(
    db: Session,
    customer: dict,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    subscriber, recipients = _resolve_notification_context(db, customer)
    merged = _load_notifications(db, subscriber=subscriber, recipients=recipients)
    total = len(merged)
    offset = (page - 1) * per_page
    notifications = merged[offset : offset + per_page]

    preferences = get_subscriber_notification_preferences(subscriber)
    return {
        "notifications": notifications,
        "page": page,
        "per_page": per_page,
        "total": total,
        "billing_notifications_enabled": preferences["billing_notifications"],
        "sms_updates_enabled": preferences["sms_updates"],
        "unread_notifications_count": total,
    }
