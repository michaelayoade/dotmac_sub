"""Timestamp-monotonic provider receipt projection for Team Inbox."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import InboxMessage, InboxMessageDirection

_DELIVERY_STATUS_RANK = {
    "accepted": 1,
    "sent": 1,
    "delivered": 2,
    "read": 3,
    "failed": 3,
}
_TERMINAL_DELIVERY_STATUSES = {"delivered", "read", "failed"}


def _delivery_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def apply_whatsapp_delivery_status(
    db: Session,
    status_item: dict[str, Any],
) -> dict[str, object]:
    raw_observed_at = status_item.get("timestamp")
    try:
        observed_at = datetime.fromtimestamp(float(str(raw_observed_at)), tz=UTC)
    except (TypeError, ValueError, OSError):
        observed_at = datetime.fromtimestamp(0, tz=UTC)
    raw_errors = status_item.get("errors")
    errors: list[object] = raw_errors if isinstance(raw_errors, list) else []
    error_codes = tuple(
        str(item.get("code"))[:80]
        for item in errors
        if isinstance(item, dict) and item.get("code") is not None
    )
    return apply_delivery_receipt(
        db,
        provider="meta_cloud_api",
        provider_message_id=str(status_item["message_id"]),
        status=str(status_item["status"]),
        observed_at=observed_at,
        recipient_id=(
            str(status_item["recipient_id"])
            if status_item.get("recipient_id")
            else None
        ),
        error_codes=error_codes,
    )


def apply_delivery_receipt(
    db: Session,
    *,
    provider: str,
    provider_message_id: str,
    status: str,
    observed_at: datetime,
    recipient_id: str | None = None,
    error_codes: tuple[str, ...] = (),
    observation_id: UUID | None = None,
) -> dict[str, object]:
    """Project a normalized, timestamp-monotonic delivery receipt."""

    clean_status = status.strip().lower()
    if clean_status not in _DELIVERY_STATUS_RANK:
        return {
            "kind": "ignored_unknown_status",
            "provider_message_id": provider_message_id,
            "status": clean_status,
        }
    message = (
        db.query(InboxMessage)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(InboxMessage.external_message_id == provider_message_id)
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    if message is None:
        return {
            "kind": "not_found",
            "provider_message_id": provider_message_id,
            "status": clean_status,
        }

    metadata = dict(message.metadata_ or {})
    current_status = str(metadata.get("delivery_status") or "").strip().lower()
    current_at = _delivery_timestamp(metadata.get("delivery_status_at"))
    observed_at = observed_at.astimezone(UTC)
    current_rank = _DELIVERY_STATUS_RANK.get(current_status, 0)
    incoming_rank = _DELIVERY_STATUS_RANK[clean_status]
    reordered = current_at is not None and observed_at < current_at
    rank_regression = incoming_rank < current_rank
    terminal_conflict = (
        current_status in _TERMINAL_DELIVERY_STATUSES
        and clean_status in _TERMINAL_DELIVERY_STATUSES
        and clean_status != current_status
    )
    if reordered or (rank_regression and not terminal_conflict):
        return {
            "kind": "ignored_reordered",
            "message_id": str(message.id),
            "provider_message_id": provider_message_id,
            "status": current_status,
        }
    if (
        current_status == clean_status
        and current_at is not None
        and observed_at <= current_at
    ):
        return {
            "kind": "duplicate",
            "message_id": str(message.id),
            "provider_message_id": provider_message_id,
            "status": current_status,
        }

    history = metadata.get("delivery_status_history")
    if not isinstance(history, list):
        history = []
    event = {
        "provider": provider,
        "status": clean_status,
        "observed_at": observed_at.isoformat(),
        "recipient_id": recipient_id,
        "error_codes": list(error_codes),
        "observation_id": str(observation_id) if observation_id else None,
    }
    history.append({key: value for key, value in event.items() if value is not None})
    metadata["delivery_status"] = clean_status
    metadata["delivery_status_at"] = observed_at.isoformat()
    metadata["delivery_provider"] = provider
    metadata["delivery_recipient_id"] = recipient_id
    if error_codes:
        metadata["delivery_error_codes"] = list(error_codes)
    metadata.pop("delivery_errors", None)
    metadata["delivery_status_history"] = history[-20:]
    message.metadata_ = metadata
    return {
        "kind": "updated",
        "message_id": str(message.id),
        "provider_message_id": provider_message_id,
        "status": clean_status,
    }
