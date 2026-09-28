"""Event adapter for capability-bound outbound integration delivery."""

import logging

from sqlalchemy.orm import Session

from app.services.events.handlers.owner_session import owner_session
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
        from app.services.integrations import meta_capi_lead
        from app.services.marketing_conversion_projection import (
            handles_conversion_event,
            project_conversion_event,
        )
        from app.services.owner_commands import CommandContext

        # Stage the independently configured Meta delivery first. If the legacy
        # conversion projection is unavailable, the dispatcher must still retain
        # this durable, idempotent delivery and retry the failing projection.
        with owner_session(db) as owner_db:
            meta_lead = meta_capi_lead.stage_lead(
                owner_db,
                meta_capi_lead.StageMetaCapiLeadCommand(
                    context=CommandContext.system(
                        actor="events.webhook_handler",
                        scope=meta_capi_lead.META_CAPI_STAGE_SCOPE,
                        reason=event.event_type.value,
                        command_id=event.event_id,
                        correlation_id=event.event_id,
                        causation_id=event.event_id,
                        idempotency_key=f"meta-capi-event:{event.event_id}",
                    ),
                    event=event,
                ),
            )
        try:
            meta_capi_lead.queue_delivery(meta_lead)
        except Exception:
            # The committed IntegrationDelivery plus the periodic redrive task
            # are authoritative. Broker availability cannot roll back a Lead.
            logger.exception("Failed to wake Meta CAPI Lead delivery worker")
        if handles_conversion_event(event):
            with owner_session(db) as owner_db:
                project_conversion_event(
                    owner_db,
                    event=event,
                    context=CommandContext.system(
                        actor="events.webhook_handler",
                        scope="marketing:conversion-projection",
                        reason=event.event_type.value,
                        command_id=event.event_id,
                        correlation_id=event.event_id,
                        causation_id=event.event_id,
                        idempotency_key=f"event:{event.event_id}",
                    ),
                )
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
