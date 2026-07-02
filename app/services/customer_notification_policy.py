"""Shared policy helpers for customer-facing notifications."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.subscriber import Subscriber, SubscriberContact
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import resolve_customer_identity
from app.services.settings_spec import resolve_value

NotificationCategory = str

_BILLING_PREFIXES = ("invoice_", "payment_")
_SERVICE_PREFIXES = (
    "subscription_",
    "service_order_",
    "provisioning_",
    "appointment_",
    "ont_",
)
_ACCOUNT_PREFIXES = ("subscriber_", "profile_")
_USAGE_PREFIXES = ("usage_",)


def _metadata_flag(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def get_subscriber_notification_preferences(
    subscriber: Subscriber | None,
) -> dict[str, bool]:
    metadata = dict(subscriber.metadata_ or {}) if subscriber else {}
    categories = ("billing", "service", "account", "usage", "general")
    return {
        "billing_notifications": _metadata_flag(
            metadata.get("billing_notifications"),
            True,
        ),
        "sms_updates": _metadata_flag(metadata.get("sms_updates"), True),
        "push_notifications": _metadata_flag(metadata.get("push_notifications"), True),
        **{
            f"{category}_notifications": _metadata_flag(
                metadata.get(f"{category}_notifications"),
                True,
            )
            for category in categories
        },
    }


def resolve_notification_category(identifier: str | None) -> NotificationCategory:
    normalized = (identifier or "").strip().lower()
    try:
        from app.services.events.handlers.notification import EVENT_NOTIFICATION_SPECS

        for event_type, spec in EVENT_NOTIFICATION_SPECS.items():
            if normalized in {event_type.value, spec.template_code}:
                return spec.category
    except Exception:
        pass
    for prefix in _BILLING_PREFIXES:
        if normalized.startswith(prefix):
            return "billing"
    for prefix in _SERVICE_PREFIXES:
        if normalized.startswith(prefix):
            return "service"
    for prefix in _ACCOUNT_PREFIXES:
        if normalized.startswith(prefix):
            return "account"
    for prefix in _USAGE_PREFIXES:
        if normalized.startswith(prefix):
            return "usage"
    return "general"


def resolve_subscriber_id_for_recipient(
    db: Session,
    recipient: str | None,
) -> UUID | None:
    resolution = resolve_customer_identity(db, recipient)
    return resolution.subscriber_id if resolution.matched else None


def _contact_preference_rows(
    db: Session,
    *,
    subscriber_id: UUID,
    recipient: str | None,
) -> list[SubscriberContact]:
    normalized_email = normalize_email_identifier(recipient)
    normalized_phone = normalize_phone_identifier(recipient)
    if not normalized_email and not normalized_phone:
        return []

    rows = db.scalars(
        select(SubscriberContact).where(
            SubscriberContact.subscriber_id == subscriber_id
        )
    ).all()
    return [
        contact
        for contact in rows
        if (
            normalized_email
            and normalize_email_identifier(contact.email) == normalized_email
        )
        or (
            normalized_phone
            and (
                normalize_phone_identifier(contact.phone) == normalized_phone
                or normalize_phone_identifier(contact.whatsapp) == normalized_phone
            )
        )
    ]


def is_notification_enabled_for_subscriber(
    db: Session,
    *,
    subscriber_id: UUID | None,
    channel: NotificationChannel | str,
    category: NotificationCategory,
    recipient: str | None = None,
) -> bool:
    if subscriber_id is None:
        return True

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        return True

    normalized_channel = (
        (channel.value if isinstance(channel, NotificationChannel) else str(channel))
        .strip()
        .lower()
    )
    preferences = get_subscriber_notification_preferences(subscriber)

    category_key = f"{str(category or 'general').strip().lower()}_notifications"
    if category_key in preferences and not preferences[category_key]:
        return False
    if category == "billing" and not preferences["billing_notifications"]:
        return False
    if normalized_channel in {"sms", "whatsapp"} and not preferences["sms_updates"]:
        return False
    if normalized_channel == "push" and not preferences["push_notifications"]:
        return False

    matching_contacts = _contact_preference_rows(
        db,
        subscriber_id=subscriber.id,
        recipient=recipient,
    )
    if matching_contacts and not any(
        contact.receives_notifications for contact in matching_contacts
    ):
        return False

    return True


def _setting_bool(db: Session, key: str, default: bool = False) -> bool:
    value = resolve_value(db, SettingDomain.notification, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(db: Session, key: str, default: int = 0) -> int:
    value = resolve_value(db, SettingDomain.notification, key)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _parse_time(value: object, fallback: time) -> time:
    try:
        hour, minute = str(value or "").strip().split(":", 1)
        return time(hour=int(hour), minute=int(minute), tzinfo=UTC)
    except Exception:
        return fallback


def quiet_hours_send_at(db: Session, now: datetime | None = None) -> datetime | None:
    if not _setting_bool(db, "notification_quiet_hours_enabled", False):
        return None
    now = now or datetime.now(UTC)
    start = _parse_time(
        resolve_value(db, SettingDomain.notification, "notification_quiet_hours_start"),
        time(22, 0, tzinfo=UTC),
    )
    end = _parse_time(
        resolve_value(db, SettingDomain.notification, "notification_quiet_hours_end"),
        time(7, 0, tzinfo=UTC),
    )
    current = now.timetz()
    if start <= end:
        in_quiet_hours = start <= current < end
    else:
        in_quiet_hours = current >= start or current < end
    if not in_quiet_hours:
        return None
    send_date = now.date()
    if current >= start and start > end:
        send_date = send_date + timedelta(days=1)
    return datetime.combine(send_date, end, tzinfo=UTC)


def has_recent_notification(
    db: Session,
    *,
    subscriber_id: UUID | None,
    channel: NotificationChannel | str,
    event_type: str | None,
    category: str | None,
    recipient: str | None,
    now: datetime | None = None,
) -> bool:
    window_minutes = _setting_int(db, "notification_dedupe_window_minutes", 0)
    if window_minutes <= 0:
        return False
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=window_minutes)
    normalized_channel = (
        channel
        if isinstance(channel, NotificationChannel)
        else NotificationChannel(str(channel))
    )
    query = (
        db.query(Notification.id)
        .filter(Notification.channel == normalized_channel)
        .filter(Notification.created_at >= cutoff)
        .filter(Notification.status != NotificationStatus.canceled)
    )
    if subscriber_id is not None:
        query = query.filter(Notification.subscriber_id == subscriber_id)
    if event_type:
        query = query.filter(Notification.event_type == event_type)
    if category:
        query = query.filter(Notification.category == category)
    if recipient:
        query = query.filter(Notification.recipient == recipient)
    return query.first() is not None


def visible_notification_count(items: Iterable[object]) -> int:
    return sum(1 for _ in items)
