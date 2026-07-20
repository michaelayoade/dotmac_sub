"""Event-driven payment arrangement progression.

When a billing payment is received for an account with an active payment
arrangement, the matching installments are advanced automatically so the
arrangement can progress (and complete) without manual admin action.
"""

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset({EventType.payment_received})


class ArrangementHandler:
    """Handler that applies received payments to active arrangements."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type != EventType.payment_received:
            return
        if not event.account_id:
            return

        payload = event.payload or {}
        status = payload.get("status")
        if status not in (None, "succeeded"):
            return

        try:
            amount = Decimal(str(payload.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            return
        if amount <= 0:
            return

        from app.services.payment_arrangements import apply_payment_to_arrangement

        result = apply_payment_to_arrangement(
            db,
            str(event.account_id),
            amount,
            payment_id=payload.get("payment_id"),
        )
        if result and result.get("installments_paid"):
            logger.info(
                "payment.received advanced arrangement %s by %d installment(s)",
                result.get("arrangement_id"),
                result.get("installments_paid"),
            )
