"""Project committed lifecycle facts into downstream sales-service owners.

The handler is deliberately orchestration-only: vendor verification and
service-order completion remain facts owned by their originating services,
while this adapter asks the next canonical owner to apply the consequence.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.vendor_project_verified,
        EventType.service_order_completed,
    }
)


class SalesLifecycleProjectionHandler:
    """Request idempotent downstream lifecycle consequences after commit."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.vendor_project_verified:
            self._release_verified_implementation(db, event)
        elif event.event_type == EventType.service_order_completed:
            self._prepare_customer_experience_handoff(db, event)

    @staticmethod
    def _release_verified_implementation(db: Session, event: Event) -> None:
        installation_project_id = event.payload.get("project_id")
        if not installation_project_id:
            logger.warning(
                "vendor verification event %s has no installation project id",
                event.event_id,
            )
            return
        from app.services import sales_fulfillment

        sales_fulfillment.release_verified_implementation(
            db,
            installation_project_id=coerce_uuid(installation_project_id),
            verification_event_id=event.event_id,
            actor_id=str(event.actor or "sales.lifecycle_projection"),
            commit=False,
        )

    @staticmethod
    def _prepare_customer_experience_handoff(db: Session, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get(
            "service_order_id"
        )
        # Non-sales repair/reprovisioning orders intentionally have no CX
        # sales handoff. The owner validates all other structural context.
        if not service_order_id or not event.payload.get("sales_order_id"):
            return
        from app.services import customer_experience_handoffs

        customer_experience_handoffs.ensure_ready_for_service_order(
            db,
            service_order_id=coerce_uuid(service_order_id),
            actor_id="sales.lifecycle_projection",
        )
