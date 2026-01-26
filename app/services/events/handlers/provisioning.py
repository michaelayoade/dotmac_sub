"""Provisioning handler for event-driven automation."""

import logging

from sqlalchemy.orm import Session

from app.models.provisioning import ProvisioningRun, ProvisioningRunStatus, ServiceOrder
from app.schemas.provisioning import ProvisioningRunStart
from app.services import provisioning as provisioning_service
from app.services.common import coerce_uuid
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)


class ProvisioningHandler:
    """Handler that triggers provisioning workflows on key events."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.subscription_activated:
            self._handle_subscription_activated(db, event)
        elif event.event_type == EventType.service_order_assigned:
            self._handle_service_order_assigned(db, event)

    def _handle_subscription_activated(self, db: Session, event: Event) -> None:
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.warning(
                "Skipping auto IP allocation: missing subscription_id in event payload."
            )
            return
        try:
            provisioning_service.ensure_ip_assignments_for_subscription(
                db, str(subscription_id)
            )
        except Exception as exc:
            logger.warning(
                "Auto IP allocation failed for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_service_order_assigned(self, db: Session, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get("service_order_id")
        if not service_order_id:
            logger.warning(
                "Skipping provisioning run: missing service_order_id in event payload."
            )
            return
        try:
            order_uuid = coerce_uuid(service_order_id)
        except (TypeError, ValueError):
            logger.warning("Skipping provisioning run: invalid service_order_id.")
            return
        service_order = db.get(ServiceOrder, order_uuid)
        if not service_order:
            logger.warning(
                "Skipping provisioning run: service order %s not found.",
                service_order_id,
            )
            return
        existing = (
            db.query(ProvisioningRun)
            .filter(ProvisioningRun.service_order_id == service_order.id)
            .filter(ProvisioningRun.status != ProvisioningRunStatus.failed)
            .first()
        )
        if existing:
            logger.info(
                "Skipping provisioning run for service order %s: existing run %s with status %s.",
                service_order_id,
                existing.id,
                existing.status.value,
            )
            return
        workflow = provisioning_service.resolve_workflow_for_service_order(db, service_order)
        if not workflow:
            logger.warning(
                "Skipping provisioning run for service order %s: no active workflow found.",
                service_order_id,
            )
            return
        try:
            provisioning_service.provisioning_runs.run(
                db,
                str(workflow.id),
                ProvisioningRunStart(
                    service_order_id=service_order.id,
                    subscription_id=service_order.subscription_id,
                ),
            )
        except Exception as exc:
            logger.exception(
                "Provisioning run failed for service order %s: %s",
                service_order_id,
                exc,
            )
