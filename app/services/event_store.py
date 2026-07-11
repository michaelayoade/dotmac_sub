from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.event_store import EventHandlerAttempt, EventStatus, EventStore
from app.services.events.types import Event

_SENSITIVE_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "password",
    "secret",
    "token",
}


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if key.lower() in _SENSITIVE_KEYS
            else _sanitize_payload(val)
            for key, val in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitize_payload(item) for item in value]
    return value


def create_event_record(db: Session, event: Event) -> EventStore:
    record = EventStore(
        event_id=event.event_id,
        event_type=event.event_type.value,
        payload=_sanitize_payload(event.payload),
        status=EventStatus.processing,
        actor=event.actor,
        subscriber_id=event.subscriber_id,
        account_id=event.account_id,
        subscription_id=event.subscription_id,
        invoice_id=event.invoice_id,
        service_order_id=event.service_order_id,
    )
    db.add(record)
    db.flush()
    return record


def mark_event_completed(
    db: Session,
    record: EventStore,
    failed_handlers: list[dict[str, str]],
) -> None:
    if failed_handlers:
        record.status = EventStatus.failed
        record.failed_handlers = failed_handlers
        record.error = json.dumps([failure["error"] for failure in failed_handlers])
    else:
        record.status = EventStatus.completed
        record.failed_handlers = None
        record.error = None
    record.processed_at = datetime.now(UTC)
    db.flush()


def record_handler_attempt(
    db: Session,
    *,
    event_store_id: UUID,
    handler_name: str,
    status: str,
    error: str | None = None,
    retry_count: int = 0,
) -> EventHandlerAttempt:
    attempt = EventHandlerAttempt(
        event_store_id=event_store_id,
        handler_name=handler_name,
        status=status,
        error=error,
        retry_count=retry_count,
    )
    db.add(attempt)
    db.flush()
    return attempt


def mark_retry_started(db: Session, record: EventStore) -> None:
    record.retry_count += 1
    record.status = EventStatus.processing
    db.flush()


def failed_handler_names(record: EventStore) -> set[str]:
    attempts = getattr(record, "handler_attempts", None) or []
    names = {
        attempt.handler_name
        for attempt in attempts
        if getattr(attempt, "status", None) == "failed"
    }
    if names:
        return names
    if record.failed_handlers:
        return {failure["handler"] for failure in record.failed_handlers}
    return set()


def list_retryable_failed_events(
    db: Session,
    *,
    max_retries: int,
    max_age_hours: int,
    limit: int,
) -> list[EventStore]:
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    return (
        db.query(EventStore)
        .filter(EventStore.status == EventStatus.failed)
        .filter(EventStore.retry_count < max_retries)
        .filter(EventStore.created_at > cutoff)
        .filter(EventStore.is_active.is_(True))
        .order_by(EventStore.created_at.asc())
        .limit(limit)
        .all()
    )


def cleanup_completed_events(db: Session, *, retention_days: int) -> dict[str, int]:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    old_completed_event_ids = (
        db.query(EventStore.id)
        .filter(EventStore.status == EventStatus.completed)
        .filter(EventStore.created_at < cutoff)
        .scalar_subquery()
    )
    deleted_attempt_count = (
        db.query(EventHandlerAttempt)
        .filter(EventHandlerAttempt.event_store_id.in_(old_completed_event_ids))
        .delete(synchronize_session=False)
    )
    deleted_count = (
        db.query(EventStore)
        .filter(EventStore.id.in_(old_completed_event_ids))
        .delete(synchronize_session=False)
    )
    return {
        "deleted": int(deleted_count or 0),
        "handler_attempts_deleted": int(deleted_attempt_count or 0),
    }


def mark_stale_processing_events(db: Session, *, stale_minutes: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    stuck_events = (
        db.query(EventStore)
        .filter(EventStore.status == EventStatus.processing)
        .filter(EventStore.updated_at < cutoff)
        .filter(EventStore.is_active.is_(True))
        .all()
    )
    for record in stuck_events:
        record.status = EventStatus.failed
        record.error = "Event processing timed out (marked as stale)"
    db.flush()
    return len(stuck_events)
