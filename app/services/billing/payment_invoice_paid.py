"""Typed paid-transition consequence for the first payment email cutover."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.payment_email_cutover import composition_enabled


@dataclass(frozen=True)
class InvoicePaidPaymentCause:
    invoice_id: UUID
    payment_id: UUID
    allocation_id: UUID
    ledger_entry_id: UUID
    subscriber_id: UUID
    previous_status: InvoiceStatus
    amount: Decimal

    def event_payload(self) -> dict[str, str]:
        return {
            "invoice_id": str(self.invoice_id),
            "payment_id": str(self.payment_id),
            "allocation_id": str(self.allocation_id),
            "ledger_entry_id": str(self.ledger_entry_id),
            "previous_status": self.previous_status.value,
            "amount": str(self.amount),
            "source": "payment_settlement",
        }


def stage_invoice_paid_payment_consequence(
    db: Session,
    *,
    invoice: Invoice,
    allocation: PaymentAllocation,
    previous_status: InvoiceStatus,
) -> None:
    """Stage the real non-paid→paid transition in the settlement transaction.

    Credit notes, repairs, historical imports, multi-invoice payments and replay
    do not acquire payment causation. The activation row gates producer and
    handler together; a producer-only deployment cannot generate extra email.
    """
    if (
        previous_status is InvoiceStatus.paid
        or invoice.status is not InvoiceStatus.paid
        or not composition_enabled(db)
    ):
        return
    if allocation.invoice_id != invoice.id:
        raise DomainError(
            code="payment_invoice_paid.invalid_scope",
            message="Paid-invoice consequence allocation has a different invoice",
            retryable=False,
        )
    payment = db.get(Payment, allocation.payment_id)
    active = db.scalars(
        select(PaymentAllocation).where(
            PaymentAllocation.payment_id == allocation.payment_id,
            PaymentAllocation.is_active.is_(True),
        )
    ).all()
    if (
        payment is None
        or payment.status is not PaymentStatus.succeeded
        or not payment.is_active
        or payment.account_id != invoice.account_id
        or allocation.invoice_id != invoice.id
        or allocation.ledger_entry_id is None
        or len(active) != 1
        or active[0].id != allocation.id
    ):
        return
    ledger = db.get(LedgerEntry, allocation.ledger_entry_id)
    if (
        ledger is None
        or not ledger.is_active
        or ledger.payment_id != payment.id
        or ledger.invoice_id != invoice.id
        or ledger.account_id != invoice.account_id
        or ledger.entry_type is not LedgerEntryType.credit
        or ledger.source is not LedgerSource.payment
        or ledger.amount != allocation.amount
    ):
        return
    cause = InvoicePaidPaymentCause(
        invoice.id,
        payment.id,
        allocation.id,
        allocation.ledger_entry_id,
        invoice.account_id,
        previous_status,
        allocation.amount,
    )
    event_id = uuid5(
        NAMESPACE_URL,
        f"dotmac-sub:payment-invoice-paid:{cause.invoice_id}:{cause.payment_id}:{cause.ledger_entry_id}",
    )
    emit_event(
        db,
        EventType.invoice_paid,
        cause.event_payload(),
        event_id=event_id,
        account_id=cause.subscriber_id,
        invoice_id=cause.invoice_id,
        defer_until_commit=True,
    )
