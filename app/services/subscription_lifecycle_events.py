from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services.events.types import Event


def parse_subscription_status(value: str | None) -> SubscriptionStatus | None:
    if value is None:
        return None
    try:
        return SubscriptionStatus(value)
    except ValueError:
        return None


def create_from_event(
    db: Session,
    event: Event,
    *,
    lifecycle_type: LifecycleEventType,
) -> SubscriptionLifecycleEvent:
    lifecycle_event = SubscriptionLifecycleEvent(
        subscription_id=event.subscription_id,
        event_type=lifecycle_type,
        from_status=parse_subscription_status(event.payload.get("from_status")),
        to_status=parse_subscription_status(event.payload.get("to_status")),
        reason=event.payload.get("reason"),
        notes=event.payload.get("notes"),
        metadata_={
            "event_id": str(event.event_id),
            "payload": event.payload,
        },
        actor=event.actor,
    )
    db.add(lifecycle_event)
    db.flush()
    return lifecycle_event
