"""Authoritative reconciliation of ZeptoMail delivery observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.services.communication_intents import record_delivery_outcome
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.zeptomail_delivery_reconciliation"
CONCERN = "ZeptoMail provider delivery reconciliation"
_APPLY_STATUS = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="apply_zeptomail_delivery_status",
)

_SUBMITTED_STATUSES = frozenset({"accepted", "processed", "queued", "submitted"})
_DELIVERED_STATUSES = frozenset({"delivered"})
_BOUNCED_STATUSES = frozenset(
    {
        "bounce",
        "bounced",
        "hard bounce",
        "hardbounce",
        "soft bounce",
        "softbounce",
    }
)
_FAILED_STATUSES = frozenset({"failed", "failure", "process failed", "mailfailure"})
_TERMINAL = frozenset(
    {
        NotificationStatus.delivered,
        NotificationStatus.bounced,
        NotificationStatus.failed,
    }
)


@dataclass(frozen=True, slots=True)
class ApplyZeptoMailDeliveryStatusCommand:
    context: CommandContext
    notification_id: UUID
    provider_status: str
    observed_at: datetime
    email_reference: str | None = None
    request_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyZeptoMailDeliveryStatusOutcome:
    kind: str
    notification_id: UUID
    status: NotificationStatus | None


def _normalized_status(value: str) -> str:
    return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def _local_status(provider_status: str) -> NotificationStatus | None:
    normalized = _normalized_status(provider_status)
    if normalized in _SUBMITTED_STATUSES:
        return NotificationStatus.submitted
    if normalized in _DELIVERED_STATUSES:
        return NotificationStatus.delivered
    if normalized in _BOUNCED_STATUSES:
        return NotificationStatus.bounced
    if normalized in _FAILED_STATUSES:
        return NotificationStatus.failed
    return None


def _delivery_status(status: NotificationStatus) -> DeliveryStatus:
    if status is NotificationStatus.submitted:
        return DeliveryStatus.accepted
    if status is NotificationStatus.delivered:
        return DeliveryStatus.delivered
    if status is NotificationStatus.bounced:
        return DeliveryStatus.bounced
    return DeliveryStatus.failed


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _stored_observed_at(notification: Notification) -> datetime | None:
    raw = (notification.metadata_ or {}).get("provider_status_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def apply_zeptomail_delivery_status(
    db: Session,
    command: ApplyZeptoMailDeliveryStatusCommand,
) -> ApplyZeptoMailDeliveryStatusOutcome:
    """Apply one provider fact without allowing stale status regression."""

    def operation() -> ApplyZeptoMailDeliveryStatusOutcome:
        notification = (
            db.query(Notification)
            .filter(Notification.id == command.notification_id)
            .with_for_update()
            .one_or_none()
        )
        if notification is None or notification.channel != NotificationChannel.email:
            return ApplyZeptoMailDeliveryStatusOutcome(
                kind="not_found",
                notification_id=command.notification_id,
                status=None,
            )
        target_status = _local_status(command.provider_status)
        if target_status is None:
            return ApplyZeptoMailDeliveryStatusOutcome(
                kind="ignored_unknown_status",
                notification_id=notification.id,
                status=notification.status,
            )
        observed_at = _as_utc(command.observed_at)
        current_observed_at = _stored_observed_at(notification)
        if current_observed_at is not None and observed_at < current_observed_at:
            return ApplyZeptoMailDeliveryStatusOutcome(
                kind="ignored_stale",
                notification_id=notification.id,
                status=notification.status,
            )
        if (
            notification.status in _TERMINAL
            and target_status is NotificationStatus.submitted
        ):
            return ApplyZeptoMailDeliveryStatusOutcome(
                kind="ignored_regression",
                notification_id=notification.id,
                status=notification.status,
            )

        clean_reason = str(command.reason or "").strip()[:1000] or None
        metadata = dict(notification.metadata_ or {})
        metadata.update(
            {
                "delivery_provider": "zeptomail",
                "provider_status": _normalized_status(command.provider_status),
                "provider_status_at": observed_at.isoformat(),
                "provider_email_reference": command.email_reference,
                "provider_request_id": command.request_id,
            }
        )
        metadata.pop("provider_status_next_check_at", None)
        notification.metadata_ = metadata
        notification.status = target_status
        notification.last_error = (
            clean_reason
            if target_status in {NotificationStatus.failed, NotificationStatus.bounced}
            else None
        )
        if target_status is NotificationStatus.delivered:
            notification.sent_at = observed_at

        delivery = None
        if command.email_reference:
            delivery = (
                db.query(NotificationDelivery)
                .filter(NotificationDelivery.provider == "zeptomail")
                .filter(
                    NotificationDelivery.provider_message_id
                    == command.email_reference[:200]
                )
                .one_or_none()
            )
        if delivery is None:
            delivery = NotificationDelivery(
                notification_id=notification.id,
                provider="zeptomail",
                provider_message_id=(
                    command.email_reference[:200] if command.email_reference else None
                ),
                status=_delivery_status(target_status),
            )
            db.add(delivery)
        delivery.status = _delivery_status(target_status)
        delivery.response_code = _normalized_status(command.provider_status)[:60]
        delivery.response_body = clean_reason
        delivery.occurred_at = observed_at
        record_delivery_outcome(db, notification)
        db.flush()
        return ApplyZeptoMailDeliveryStatusOutcome(
            kind="updated",
            notification_id=notification.id,
            status=notification.status,
        )

    return execute_owner_command(
        db,
        definition=_APPLY_STATUS,
        context=command.context,
        operation=operation,
    )
