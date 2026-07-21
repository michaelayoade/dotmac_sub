"""Cash-first orchestration for verified provider invoice payments.

Provider verification proves money, not invoice eligibility. Confirmed cash is
therefore becomes net unallocated account credit before the invoice allocation
owner is invoked. The legacy root commits that cash boundary first; a wider
coordinator stages cash plus allocation or reconciliation evidence atomically.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Payment,
    PaymentAllocation,
    PaymentAllocationReconciliationException,
)
from app.models.event_store import EventStore
from app.schemas.billing import (
    PaymentAllocationConfirm,
    PaymentAllocationPreviewRequest,
)
from app.services import billing as billing_service
from app.services.common import round_money, to_decimal
from app.services.events import emit_event
from app.services.events.types import (
    AccountCreditApplicationState,
    AccountCreditFundingOrigin,
    EventType,
)
from app.services.owner_commands import execute_owner_savepoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VerifiedInvoiceSettlementResult:
    payment: Payment
    allocation: PaymentAllocation | None
    reconciliation_exception: PaymentAllocationReconciliationException | None
    payment_created: bool


@dataclass(frozen=True, slots=True)
class VerifiedInvoiceSettlementCommand:
    """Exact verified cash and intended-invoice evidence."""

    account_id: UUID
    invoice_id: UUID
    topup_intent_id: UUID | None
    provider_id: UUID
    provider_reference: str
    external_id: str
    gross_amount: Decimal
    provider_fee: Decimal
    net_amount: Decimal
    currency: str
    memo: str
    paid_at: datetime | None = None


class _SettlementBoundary(str, Enum):
    legacy_root = "legacy_root"
    participant = "participant"


def _record_account_credit_application_completed(
    db: Session,
    *,
    payment: Payment,
    applied_amount: Decimal,
    allocation_state: AccountCreditApplicationState,
    boundary: _SettlementBoundary,
) -> None:
    """Record the funding consequence once within the active owner boundary."""

    if payment.account_id is None or payment.settlement is None:
        return
    origin = AccountCreditFundingOrigin.verified_invoice_payment
    existing_event = db.scalar(
        select(EventStore.id).where(
            EventStore.event_type == EventType.account_credit_deposited.value,
            EventStore.account_id == payment.account_id,
            EventStore.payload["payment_id"].as_string() == str(payment.id),
            EventStore.payload["origin"].as_string() == origin.value,
        )
    )
    if existing_event is not None:
        return
    emit_event(
        db,
        EventType.account_credit_deposited,
        {
            "schema_version": 1,
            "payment_id": str(payment.id),
            "amount": str(payment.settlement.amount),
            "currency": payment.settlement.currency,
            "applied_amount": str(round_money(applied_amount)),
            "allocation_state": allocation_state.value,
            "origin": origin.value,
        },
        account_id=payment.account_id,
    )
    if boundary is _SettlementBoundary.legacy_root:
        db.commit()
    else:
        db.flush()


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
    return _settle_verified_invoice_payment(
        db,
        VerifiedInvoiceSettlementCommand(
            account_id=account_id,
            invoice_id=invoice_id,
            topup_intent_id=topup_intent_id,
            provider_id=provider_id,
            provider_reference=provider_reference,
            external_id=external_id,
            gross_amount=gross_amount,
            provider_fee=provider_fee,
            net_amount=net_amount,
            currency=currency,
            memo=memo,
            paid_at=paid_at,
        ),
        boundary=_SettlementBoundary.legacy_root,
    )


def stage_verified_invoice_payment(
    db: Session,
    command: VerifiedInvoiceSettlementCommand,
) -> VerifiedInvoiceSettlementResult:
    """Stage cash, allocation, or exception evidence in a wider transaction."""

    return _settle_verified_invoice_payment(
        db,
        command,
        boundary=_SettlementBoundary.participant,
    )


def _settle_verified_invoice_payment(
    db: Session,
    command: VerifiedInvoiceSettlementCommand,
    *,
    boundary: _SettlementBoundary,
) -> VerifiedInvoiceSettlementResult:
    if boundary is _SettlementBoundary.participant:
        settlement_result = billing_service.payments.stage_verified_provider_settlement(
            db,
            account_id=command.account_id,
            provider_id=command.provider_id,
            external_id=command.external_id,
            gross_amount=command.gross_amount,
            provider_fee=command.provider_fee,
            net_amount=command.net_amount,
            currency=command.currency,
            memo=command.memo,
            paid_at=command.paid_at,
        )
    else:
        settlement_result = (
            billing_service.payments.record_verified_provider_settlement(
                db,
                account_id=command.account_id,
                provider_id=command.provider_id,
                external_id=command.external_id,
                gross_amount=command.gross_amount,
                provider_fee=command.provider_fee,
                net_amount=command.net_amount,
                currency=command.currency,
                memo=command.memo,
                paid_at=command.paid_at,
            )
        )
    payment = settlement_result.payment
    payment_id = payment.id
    existing_allocation = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.invoice_id == command.invoice_id)
        .filter(PaymentAllocation.is_active.is_(True))
        .first()
    )
    if existing_allocation is not None:
        _resolve_allocation_exception(
            db,
            command=command,
            boundary=boundary,
            payment_id=payment.id,
        )
        _record_account_credit_application_completed(
            db,
            payment=payment,
            applied_amount=round_money(to_decimal(existing_allocation.amount)),
            allocation_state=AccountCreditApplicationState.allocated,
            boundary=boundary,
        )
        return VerifiedInvoiceSettlementResult(
            payment=payment,
            allocation=existing_allocation,
            reconciliation_exception=None,
            payment_created=not settlement_result.idempotent_replay,
        )

    try:
        invoice = billing_service.invoices.get(db, str(command.invoice_id))
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
            _resolve_allocation_exception(
                db,
                command=command,
                boundary=boundary,
                payment_id=payment.id,
            )
            _record_account_credit_application_completed(
                db,
                payment=payment,
                applied_amount=Decimal("0.00"),
                allocation_state=AccountCreditApplicationState.no_allocatable_balance,
                boundary=boundary,
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
                invoice_id=command.invoice_id,
                amount=amount,
            ),
        )
        confirmation = PaymentAllocationConfirm(
            payment_id=payment.id,
            invoice_id=command.invoice_id,
            amount=amount,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=_allocation_key(
                payment_id=payment.id,
                invoice_id=command.invoice_id,
                provider_reference=command.provider_reference,
            ),
        )
        if boundary is _SettlementBoundary.participant:
            # Isolate the optional invoice consequence so its partial writes
            # cannot contaminate confirmed cash or reconciliation evidence.
            allocation_result = execute_owner_savepoint(
                db,
                lambda: billing_service.payment_allocations.stage_confirm(
                    db,
                    confirmation,
                ),
            )
        else:
            allocation_result = billing_service.payment_allocations.confirm(
                db,
                confirmation,
            )
        _resolve_allocation_exception(
            db,
            command=command,
            boundary=boundary,
            payment_id=payment.id,
        )
        _record_account_credit_application_completed(
            db,
            payment=payment,
            applied_amount=round_money(to_decimal(allocation_result.allocation.amount)),
            allocation_state=AccountCreditApplicationState.allocated,
            boundary=boundary,
        )
        return VerifiedInvoiceSettlementResult(
            payment=payment,
            allocation=allocation_result.allocation,
            reconciliation_exception=None,
            payment_created=not settlement_result.idempotent_replay,
        )
    except (HTTPException, ValueError) as exc:
        logger.warning(
            "Verified provider payment %s retained as account credit because "
            "invoice allocation failed",
            payment_id,
            exc_info=True,
        )
        exception = _record_allocation_exception(
            db,
            command=command,
            boundary=boundary,
            payment_id=payment_id,
            error=exc,
        )
    except Exception as exc:
        if boundary is _SettlementBoundary.legacy_root:
            db.rollback()
        logger.warning(
            "Verified provider payment %s retained as account credit because "
            "invoice allocation failed",
            payment_id,
            exc_info=True,
        )
        exception = _record_allocation_exception(
            db,
            command=command,
            boundary=boundary,
            payment_id=payment_id,
            error=exc,
        )
    durable_payment = db.get(Payment, payment_id)
    if durable_payment is None:
        raise RuntimeError(
            "Verified payment disappeared while recording allocation exception"
        )
    _record_account_credit_application_completed(
        db,
        payment=durable_payment,
        applied_amount=Decimal("0.00"),
        allocation_state=AccountCreditApplicationState.retained_as_account_credit,
        boundary=boundary,
    )
    return VerifiedInvoiceSettlementResult(
        payment=durable_payment,
        allocation=None,
        reconciliation_exception=exception,
        payment_created=not settlement_result.idempotent_replay,
    )


def _resolve_allocation_exception(
    db: Session,
    *,
    command: VerifiedInvoiceSettlementCommand,
    boundary: _SettlementBoundary,
    payment_id: UUID,
) -> None:
    if boundary is _SettlementBoundary.participant:
        billing_service.payment_allocation_reconciliation_exceptions.stage_resolve(
            db,
            payment_id=payment_id,
            invoice_id=command.invoice_id,
            provider_reference=command.provider_reference,
        )
    else:
        billing_service.payment_allocation_reconciliation_exceptions.resolve(
            db,
            payment_id=payment_id,
            invoice_id=command.invoice_id,
            provider_reference=command.provider_reference,
        )


def _record_allocation_exception(
    db: Session,
    *,
    command: VerifiedInvoiceSettlementCommand,
    boundary: _SettlementBoundary,
    payment_id: UUID,
    error: Exception,
) -> PaymentAllocationReconciliationException:
    if boundary is _SettlementBoundary.participant:
        return (
            billing_service.payment_allocation_reconciliation_exceptions.stage_record(
                db,
                payment_id=payment_id,
                invoice_id=command.invoice_id,
                topup_intent_id=command.topup_intent_id,
                provider_reference=command.provider_reference,
                external_id=command.external_id,
                error=error,
            )
        )
    return billing_service.payment_allocation_reconciliation_exceptions.record(
        db,
        payment_id=payment_id,
        invoice_id=command.invoice_id,
        topup_intent_id=command.topup_intent_id,
        provider_reference=command.provider_reference,
        external_id=command.external_id,
        error=error,
    )


__all__ = [
    "VerifiedInvoiceSettlementCommand",
    "VerifiedInvoiceSettlementResult",
    "settle_verified_invoice_payment",
    "stage_verified_invoice_payment",
]
