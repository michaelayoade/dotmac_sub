"""Event adapter for capability-bound outbound integration delivery."""

import logging

from sqlalchemy.orm import Session

from app.services.events.types import Event
from app.services.integrations.delivery import (
    create_platform_deliveries_for_event,
    queue_platform_deliveries,
)
from app.services.integrations.meta_lead_conversion import (
    queue_conversion,
    stage_conversion_for_event,
)

logger = logging.getLogger(__name__)


class WebhookHandler:
    """Request typed delivery for enabled event subscriptions."""

    def handle(self, db: Session, event: Event) -> None:
        meta_conversion = stage_conversion_for_event(db, event=event)
        queue_conversion(meta_conversion)
        deliveries = create_platform_deliveries_for_event(
            db,
            event=event,
            event_type=event.event_type.value,
        )
        if not deliveries:
            logger.debug(
                "No integration subscriptions for event type %s",
                event.event_type.value,
            )
            return
        try:
            queue_platform_deliveries(deliveries, event=event)
            logger.info(
                "Queued %s integration deliveries for event %s",
                len(deliveries),
                event.event_type.value,
            )
        except Exception:
            logger.exception("Failed to queue integration delivery tasks")
