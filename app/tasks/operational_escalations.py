"""Celery tasks for operational escalation delivery."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.operational_escalations.dispatch_operational_escalation_deliveries"
)
def dispatch_operational_escalation_deliveries(
    *,
    limit: int = 100,
    max_retries: int = 3,
) -> dict[str, int]:
    from app.models.operational_escalation import OperationalDeliveryStatus
    from app.services.operational_escalation_delivery import dispatch_pending_deliveries

    with db_session_adapter.session() as session:
        deliveries = dispatch_pending_deliveries(
            session,
            limit=limit,
            max_retries=max_retries,
        )
        counts = {
            OperationalDeliveryStatus.sent: 0,
            OperationalDeliveryStatus.failed: 0,
            OperationalDeliveryStatus.suppressed: 0,
            OperationalDeliveryStatus.acknowledged: 0,
            OperationalDeliveryStatus.pending: 0,
        }
        for delivery in deliveries:
            counts[delivery.delivery_status] = (
                counts.get(delivery.delivery_status, 0) + 1
            )
        result = {
            "processed": len(deliveries),
            "sent": counts.get(OperationalDeliveryStatus.sent, 0),
            "failed": counts.get(OperationalDeliveryStatus.failed, 0),
            "suppressed": counts.get(OperationalDeliveryStatus.suppressed, 0),
            "acknowledged": counts.get(OperationalDeliveryStatus.acknowledged, 0),
            "pending": counts.get(OperationalDeliveryStatus.pending, 0),
        }
        logger.info(
            "operational escalation delivery dispatch complete",
            extra={"event": "operational_escalation_delivery_dispatch", **result},
        )
        return result
