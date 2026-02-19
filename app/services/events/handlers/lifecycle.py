"""Lifecycle handler for the event system.

Records SubscriptionLifecycleEvent records for subscription state changes.
"""

import logging

from sqlalchemy.orm import Session

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services.events.types import SUBSCRIPTION_LIFECYCLE_MAP, Event

logger = logging.getLogger(__name__)


def _parse_subscription_status(value: str | None) -> SubscriptionStatus | None:
    """Convert a string status to SubscriptionStatus enum, or None if invalid."""
    if value is None:
        return None
    try:
        return SubscriptionStatus(value)
    except ValueError:
        return None


class LifecycleHandler:
    """Handler that records subscription lifecycle events."""

    def handle(self, db: Session, event: Event) -> None:
        """Process an event by creating lifecycle records.

        Only subscription-related events that map to lifecycle types
        will create records. Other events are ignored.

        Args:
            db: Database session
            event: The event to process
        """
        # Check if this is a subscription lifecycle event
        lifecycle_type_str = SUBSCRIPTION_LIFECYCLE_MAP.get(event.event_type)
        if lifecycle_type_str is None:
            return

        # Need a subscription_id to record lifecycle
        if not event.subscription_id:
            logger.warning(
                f"Cannot record lifecycle for {event.event_type.value}: "
                "no subscription_id"
            )
            return

        # Map to LifecycleEventType enum
        try:
            lifecycle_type = LifecycleEventType(lifecycle_type_str)
        except ValueError:
            logger.warning(f"Unknown lifecycle type: {lifecycle_type_str}")
            return

        # Extract status transition from payload and convert to enums
        from_status_str = event.payload.get("from_status")
        to_status_str = event.payload.get("to_status")
        from_status = _parse_subscription_status(from_status_str)
        to_status = _parse_subscription_status(to_status_str)
        reason = event.payload.get("reason")
        notes = event.payload.get("notes")

        # Create lifecycle event
        lifecycle_event = SubscriptionLifecycleEvent(
            subscription_id=event.subscription_id,
            event_type=lifecycle_type,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            notes=notes,
            metadata_={
                "event_id": str(event.event_id),
                "payload": event.payload,
            },
            actor=event.actor,
        )
        db.add(lifecycle_event)

        logger.info(
            f"Recorded lifecycle event {lifecycle_type.value} for "
            f"subscription {event.subscription_id}"
        )
