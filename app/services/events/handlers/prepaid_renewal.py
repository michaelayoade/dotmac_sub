"""Request the canonical prepaid service consequence after credit application."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentStatus
from app.services.common import coerce_uuid
from app.services.events.types import Event, EventType
from app.services.prepaid_service_renewals import (
    apply_due_prepaid_service_after_funding_change,
)

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset({EventType.account_credit_deposited})


class PrepaidRenewalHandler:
    """Thin event adapter around the prepaid service-renewal owner."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES or event.account_id is None:
            return
        payment_id = event.payload.get("payment_id")
        if not payment_id:
            return
        payment = db.get(Payment, coerce_uuid(payment_id))
        if (
            payment is None
            or payment.account_id != event.account_id
            or payment.status != PaymentStatus.succeeded
            or not payment.is_active
            or payment.settlement is None
        ):
            return
        effective_at = payment.paid_at or payment.created_at
        if effective_at is None:
            return
        result = apply_due_prepaid_service_after_funding_change(
            db,
            account_id=event.account_id,
            effective_at=effective_at,
            funding_currency=payment.currency,
            evidence_ref=f"account-credit-event:{event.event_id}",
            trigger_payment_id=payment.id,
        )
        logger.info(
            "prepaid_renewal_after_funding_change",
            extra={
                "event": "prepaid_renewal_after_funding_change",
                "event_id": str(event.event_id),
                "payment_id": str(payment.id),
                "account_id": str(event.account_id),
                "disposition": result.disposition.value,
                "scanned": result.scanned,
                "funded": result.funded,
                "unfunded": result.unfunded,
                "already_covered": result.already_covered,
                "missing_price": result.missing_price,
                "currency_mismatch": result.currency_mismatch,
                "renewed_through": [
                    outcome.renewed_through.isoformat() for outcome in result.renewals
                ],
            },
        )


__all__ = ["HANDLED_EVENT_TYPES", "PrepaidRenewalHandler"]
