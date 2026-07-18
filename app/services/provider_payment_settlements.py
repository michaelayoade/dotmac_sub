"""Cash-first orchestration for verified provider invoice payments.

Provider verification proves money, not invoice eligibility. Confirmed cash is
therefore committed as net unallocated account credit before the invoice
allocation owner is invoked. Allocation failures are durable reconciliation
exceptions and never roll back the payment settlement.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import (
    Payment,
    PaymentAllocation,
    PaymentAllocationReconciliationException,
)
from app.schemas.billing import (
    PaymentAllocationConfirm,
    PaymentAllocationPreviewRequest,
)
from app.services import billing as billing_service
from app.services.common import round_money, to_decimal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifiedInvoiceSettlementResult:
    payment: Payment
    allocation: PaymentAllocation | None
    reconciliation_exception: PaymentAllocationReconciliationException | None
    payment_created: bool


def _allocation_key(
    *, payment_id: UUID, invoice_id: UUID, provider_reference: str
) -> str:
    material = f"{payment_id}:{invoice_id}:{provider_reference}"
    return (
        "provider-invoice-allocation-"
        + hashlib.sha256(material.encode("utf-8")).hexdigest()
    )


def settle_verified_invoice_payment(
    db: Session,
    *,
    account_id: UUID,
    invoice_id: UUID,
    topup_intent_id: UUID | None,
    provider_id: UUID,
    provider_reference: str,
    external_id: str,
    gross_amount: Decimal,
    provider_fee: Decimal,
    net_amount: Decimal,
    currency: str,
    memo: str,
    paid_at: datetime | None = None,
) -> VerifiedInvoiceSettlementResult:
    """Commit verified cash, then independently attempt its intended allocation."""
    settlement_result = billing_service.payments.record_verified_provider_settlement(
        db,
        account_id=account_id,
        provider_id=provider_id,
        external_id=external_id,
        gross_amount=gross_amount,
        provider_fee=provider_fee,
        net_amount=net_amount,
        currency=currency,
        memo=memo,
        paid_at=paid_at,
    )
    payment = settlement_result.payment
    payment_id = payment.id
    existing_allocation = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.invoice_id == invoice_id)
        .filter(PaymentAllocation.is_active.is_(True))
        .first()
    )
    if existing_allocation is not None:
        billing_service.payment_allocation_reconciliation_exceptions.resolve(
            db,
            payment_id=payment.id,
            invoice_id=invoice_id,
            provider_reference=provider_reference,
        )
        return VerifiedInvoiceSettlementResult(
            payment=payment,
            allocation=existing_allocation,
            reconciliation_exception=None,
            payment_created=not settlement_result.idempotent_replay,
        )

    try:
        invoice = billing_service.invoices.get(db, str(invoice_id))
        if invoice is None or not invoice.is_active:
            raise ValueError("Target invoice is unavailable")
        balance_due = round_money(to_decimal(invoice.balance_due or 0))
        settlement = payment.settlement
        if settlement is None:
            raise ValueError("Verified payment has no settlement evidence")
        amount = min(
            round_money(to_decimal(settlement.unallocated_amount)), balance_due
        )
        if amount <= Decimal("0.00"):
            billing_service.payment_allocation_reconciliation_exceptions.resolve(
                db,
                payment_id=payment.id,
                invoice_id=invoice_id,
                provider_reference=provider_reference,
            )
            return VerifiedInvoiceSettlementResult(
                payment=payment,
                allocation=None,
                reconciliation_exception=None,
                payment_created=not settlement_result.idempotent_replay,
            )
        preview = billing_service.payment_allocations.preview(
            db,
            PaymentAllocationPreviewRequest(
                payment_id=payment.id,
                invoice_id=invoice_id,
                amount=amount,
            ),
        )
        allocation_result = billing_service.payment_allocations.confirm(
            db,
            PaymentAllocationConfirm(
                payment_id=payment.id,
                invoice_id=invoice_id,
                amount=amount,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=_allocation_key(
                    payment_id=payment.id,
                    invoice_id=invoice_id,
                    provider_reference=provider_reference,
                ),
            ),
        )
        billing_service.payment_allocation_reconciliation_exceptions.resolve(
            db,
            payment_id=payment.id,
            invoice_id=invoice_id,
            provider_reference=provider_reference,
        )
        return VerifiedInvoiceSettlementResult(
            payment=payment,
            allocation=allocation_result.allocation,
            reconciliation_exception=None,
            payment_created=not settlement_result.idempotent_replay,
        )
    except Exception as exc:
        # Business-rule failures do not invalidate the transaction and can be
        # followed immediately by durable exception evidence. Database/runtime
        # failures need a rollback before that separate write is attempted.
        if not isinstance(exc, (HTTPException, ValueError)):
            db.rollback()
        logger.warning(
            "Verified provider payment %s retained as account credit because "
            "invoice allocation failed",
            payment_id,
            exc_info=True,
        )
        exception = billing_service.payment_allocation_reconciliation_exceptions.record(
            db,
            payment_id=payment_id,
            invoice_id=invoice_id,
            topup_intent_id=topup_intent_id,
            provider_reference=provider_reference,
            external_id=external_id,
            error=exc,
        )
        durable_payment = db.get(Payment, payment_id)
        if durable_payment is None:
            raise RuntimeError(
                "Verified payment disappeared while recording allocation exception"
            ) from exc
        return VerifiedInvoiceSettlementResult(
            payment=durable_payment,
            allocation=None,
            reconciliation_exception=exception,
            payment_created=not settlement_result.idempotent_replay,
        )
