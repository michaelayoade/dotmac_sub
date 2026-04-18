"""Billing boundary for core services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus, PaymentStatus
from app.schemas.billing import (
    InvoiceCreate,
    InvoiceLineCreate,
    PaymentAllocationApply,
    PaymentCreate,
    PaymentProviderEventIngest,
)


@dataclass(frozen=True)
class InvoiceIntent:
    account_id: UUID
    invoice_number: str | None = None
    currency: str = "NGN"
    total: Decimal = Decimal("0.00")
    memo: str | None = None
    status: InvoiceStatus = InvoiceStatus.draft
    issued_at: datetime | None = None
    due_at: datetime | None = None


@dataclass(frozen=True)
class InvoiceLineIntent:
    description: str
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0.00")
    tax_rate_id: UUID | None = None


@dataclass(frozen=True)
class PaymentIntent:
    account_id: UUID
    amount: Decimal
    currency: str = "NGN"
    provider_id: UUID | None = None
    external_id: str | None = None
    memo: str | None = None
    status: PaymentStatus = PaymentStatus.pending
    allocations: list[PaymentAllocationApply] = field(default_factory=list)


class BillingAdapter:
    """Adapter around invoices, payments, and payment gateway events."""

    def __init__(self, billing_service: Any | None = None) -> None:
        self._billing_service = billing_service

    def _service(self):
        if self._billing_service is not None:
            return self._billing_service
        from app.services import billing as billing_service

        return billing_service

    def create_invoice(self, db: Session, intent: InvoiceIntent):
        billing_service = self._service()

        payload = InvoiceCreate(
            account_id=intent.account_id,
            invoice_number=intent.invoice_number,
            currency=intent.currency,
            subtotal=intent.total,
            total=intent.total,
            balance_due=intent.total,
            status=intent.status,
            memo=intent.memo,
            issued_at=intent.issued_at,
            due_at=intent.due_at,
        )
        return billing_service.invoices.create(db, payload)

    def create_invoice_with_lines(
        self,
        db: Session,
        intent: InvoiceIntent,
        lines: list[InvoiceLineIntent],
    ):
        billing_service = self._service()

        invoice = self.create_invoice(db, intent)
        for line in lines:
            billing_service.invoice_lines.create(
                db,
                InvoiceLineCreate(
                    invoice_id=invoice.id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    tax_rate_id=line.tax_rate_id,
                ),
            )
        return invoice

    def record_payment(self, db: Session, intent: PaymentIntent):
        billing_service = self._service()

        payload = PaymentCreate(
            account_id=intent.account_id,
            provider_id=intent.provider_id,
            amount=intent.amount,
            currency=intent.currency,
            external_id=intent.external_id,
            status=intent.status,
            memo=intent.memo,
            allocations=list(intent.allocations),
        )
        return billing_service.payments.create(db, payload)

    def ingest_gateway_event(self, db: Session, payload: PaymentProviderEventIngest):
        billing_service = self._service()

        return billing_service.payment_provider_events.ingest(db, payload)


billing_adapter = BillingAdapter()
