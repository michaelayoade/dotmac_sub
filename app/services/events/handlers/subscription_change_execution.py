"""Thin payment-event adapter for service-change execution."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscription_change import SubscriptionChangeRequest
from app.services.common import coerce_uuid
from app.services.events.types import EventType
from app.services.subscription_change_execution import (
    finalize_verified_service_change,
    settle_relocation_payment,
)

HANDLED_EVENT_TYPES = frozenset(
    {EventType.payment_received, EventType.service_order_completed}
)


class SubscriptionChangeExecutionHandler:
    """Forward canonical invoice/payment evidence to the execution owner."""

    def handle(self, db: Session, event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if event.event_type == EventType.service_order_completed:
            service_order_id = event.service_order_id or (event.payload or {}).get(
                "service_order_id"
            )
            decision_id = (
                (event.payload or {}).get("evidence", {}).get("readiness_decision_id")
            )
            if not service_order_id or not decision_id:
                return
            request_id = db.scalar(
                select(SubscriptionChangeRequest.id).where(
                    SubscriptionChangeRequest.service_order_id
                    == coerce_uuid(service_order_id)
                )
            )
            if request_id is None:
                return
            finalize_verified_service_change(
                db,
                request_id=request_id,
                readiness_decision_id=coerce_uuid(decision_id),
                actor_id=str(event.actor or "provisioning-lifecycle"),
            )
            return
        invoice_id = event.invoice_id or (event.payload or {}).get("invoice_id")
        payment_id = (event.payload or {}).get("payment_id")
        if not invoice_id or not payment_id:
            return
        request_id = db.scalar(
            select(SubscriptionChangeRequest.id).where(
                SubscriptionChangeRequest.field_fee_invoice_id
                == coerce_uuid(invoice_id)
            )
        )
        if request_id is None:
            return
        settle_relocation_payment(
            db,
            request_id=request_id,
            payment_id=coerce_uuid(payment_id),
        )


__all__ = ["HANDLED_EVENT_TYPES", "SubscriptionChangeExecutionHandler"]
