"""Lifecycle handler for the event system.

Records SubscriptionLifecycleEvent records for subscription state changes.
"""

import logging

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.lifecycle import LifecycleEventType
from app.services.connectivity_reconciler import connectivity_shadow_diff
from app.services.events.types import SUBSCRIPTION_LIFECYCLE_MAP, Event
from app.services.subscription_lifecycle_events import create_from_event

logger = logging.getLogger(__name__)


class LifecycleHandler:
    """Handler that records subscription lifecycle events."""

    def _observe_connectivity_shadow_diff(self, db: Session, event: Event) -> None:
        """Record desired-vs-actual connectivity drift after a lifecycle event.

        Step 2d is observability only: lifecycle transitions trigger the
        read-only shadow diff so production can show what the reconciler would
        change before any legacy writer is absorbed. Failures are swallowed so
        lifecycle auditing is never blocked by metrics/logging.
        """
        if not event.subscription_id:
            return
        try:
            # Savepoint-isolated: the lifecycle record is already flushed by the
            # caller, so even a DB-level failure inside the read-only diff rolls
            # back only to here and never discards the audit write.
            with db.begin_nested():
                subscription = db.get(Subscription, event.subscription_id)
                if subscription and subscription.subscriber_id:
                    connectivity_shadow_diff(db, subscription.subscriber_id)
        except Exception as exc:
            logger.warning(
                "connectivity shadow-diff observation failed for subscription %s: %s",
                event.subscription_id,
                exc,
            )

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

        create_from_event(db, event, lifecycle_type=lifecycle_type)
        self._observe_connectivity_shadow_diff(db, event)

        logger.info(
            f"Recorded lifecycle event {lifecycle_type.value} for "
            f"subscription {event.subscription_id}"
        )
