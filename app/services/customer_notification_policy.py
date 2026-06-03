"""Shared policy helpers for customer-facing notifications."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.models.subscriber import Subscriber, SubscriberContact
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import resolve_customer_identity

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
    return {
        "billing_notifications": _metadata_flag(
            metadata.get("billing_notifications"),
            True,
        ),
        "sms_updates": _metadata_flag(metadata.get("sms_updates"), True),
    }


def resolve_notification_category(identifier: str | None) -> NotificationCategory:
    normalized = (identifier or "").strip().lower()
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

    if category == "billing" and not preferences["billing_notifications"]:
        return False
    if normalized_channel in {"sms", "whatsapp"} and not preferences["sms_updates"]:
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


def visible_notification_count(items: Iterable[object]) -> int:
    return sum(1 for _ in items)
