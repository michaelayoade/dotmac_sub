"""Project committed lifecycle facts into downstream sales-service owners.

The handler is deliberately orchestration-only: funding satisfaction, vendor
verification, service-order release/completion, and CX acceptance remain
facts owned by their originating services, while this adapter asks the next
canonical owner to apply the consequence. A consequence that cannot be
applied raises so the event delivery stays failed and retryable instead of a
warning log.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.sales_order_funding_satisfied,
        EventType.vendor_project_verified,
        EventType.service_order_released,
        EventType.service_order_completed,
        EventType.customer_experience_accepted,
    }
)


class SalesLifecycleProjectionHandler:
    """Request idempotent downstream lifecycle consequences after commit."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.sales_order_funding_satisfied:
            self._apply_funding_consequences(db, event)
        elif event.event_type == EventType.vendor_project_verified:
            self._release_verified_implementation(db, event)
        elif event.event_type == EventType.service_order_released:
            self._advance_released_order(db, event)
        elif event.event_type == EventType.service_order_completed:
            self._prepare_customer_experience_handoff(db, event)
        elif event.event_type == EventType.customer_experience_accepted:
            self._fulfill_sales_order(db, event)

    @staticmethod
    def _apply_funding_consequences(db: Session, event: Event) -> None:
        sales_order_id = event.payload.get("sales_order_id")
        if not sales_order_id:
            logger.warning(
                "funding satisfaction event %s has no sales order id",
                event.event_id,
            )
            return
        from app.services import sales_orders

        sales_orders.apply_funding_consequences(
            db,
            sales_order_id=coerce_uuid(sales_order_id),
            actor_id=str(event.actor or "sales.lifecycle_projection"),
            record_order_payment=bool(event.payload.get("record_order_payment", True)),
        )

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
    def _advance_released_order(db: Session, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get(
            "service_order_id"
        )
        # Non-sales repair/reprovisioning orders keep their manual
        # progression; only the sales chain auto-enters provisioning.
        if not service_order_id or not event.payload.get("sales_order_id"):
            return
        from app.models.provisioning import ServiceOrder, ServiceOrderStatus
        from app.services import service_order_lifecycle

        order = db.get(ServiceOrder, coerce_uuid(service_order_id))
        if order is None or order.status not in {
            ServiceOrderStatus.submitted,
            ServiceOrderStatus.scheduled,
        }:
            # Replay, or an operator already progressed the order.
            return
        service_order_lifecycle.transition_service_order(
            db,
            service_order_id=order.id,
            target_status=ServiceOrderStatus.provisioning,
            actor_id=str(event.actor or "sales.lifecycle_projection"),
            reason="Released implementation enters provisioning",
            event_evidence={"released_event_id": str(event.event_id)},
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

    @staticmethod
    def _fulfill_sales_order(db: Session, event: Event) -> None:
        sales_order_id = event.payload.get("sales_order_id")
        handoff_id = event.payload.get("handoff_id")
        if not sales_order_id or not handoff_id:
            logger.warning(
                "customer-experience acceptance event %s lacks sales order "
                "or handoff id",
                event.event_id,
            )
            return
        from app.services import sales_orders

        sales_orders.fulfill_from_customer_experience(
            db,
            sales_order_id=coerce_uuid(sales_order_id),
            handoff_id=coerce_uuid(handoff_id),
            actor_id=str(event.actor or "sales.lifecycle_projection"),
        )
