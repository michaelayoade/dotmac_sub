"""Purchase refund/reversal state participant in the payment owner's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentStatus, TopupIntent
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchasePeriod,
    PrepaidPeriodPurchaseStatus,
)
from app.services.domain_errors import DomainError


def _error(suffix: str, message: str) -> DomainError:
    return DomainError(
        code=f"financial.purchase_payment_recovery_state.{suffix}", message=message
    )


_OWNER = "financial.purchase_payment_recovery_state"


@dataclass(frozen=True, slots=True)
class PurchasePaymentRecoveryCommand:
    payment_id: UUID
    evidence_ref: str


def stage_purchase_payment_recovery(
    db: Session, command: PurchasePaymentRecoveryCommand
) -> None:
    """Participate in the payment owner's confirmed refund/reversal transaction."""
    payment = db.get(Payment, command.payment_id)
    if payment is None or payment.reserved_for_purchase_id is None:
        return
    if not command.evidence_ref.strip() or payment.status not in {
        PaymentStatus.refunded,
        PaymentStatus.partially_refunded,
        PaymentStatus.reversed,
    }:
        raise _error(
            "recovery_evidence_invalid",
            "Confirmed refund or reversal evidence is required.",
        )
    purchase = db.get(PrepaidPeriodPurchase, payment.reserved_for_purchase_id)
    if purchase is None:
        raise _error(
            "recovery_evidence_invalid", "Purchase payment ownership is incomplete."
        )
    other_held = db.scalar(
        select(Payment.id)
        .where(
            Payment.reserved_for_purchase_id == purchase.id,
            Payment.id != purchase.payment_id,
            Payment.status.in_(
                [PaymentStatus.succeeded, PaymentStatus.partially_refunded]
            ),
        )
        .limit(1)
    )
    if purchase.payment_id != payment.id:
        # A second genuine provider capture remains a separate receipt. Its
        # refund must never cancel the periods funded by the original receipt.
        if other_held is None and purchase.completed_at is not None:
            primary = (
                db.get(Payment, purchase.payment_id) if purchase.payment_id else None
            )
            if primary is not None and primary.status is PaymentStatus.succeeded:
                purchase.status = PrepaidPeriodPurchaseStatus.completed
                purchase.failure_code = None
                db.flush()
                return
        purchase.status = PrepaidPeriodPurchaseStatus.review_required
        purchase.failure_code = f"{_OWNER}.additional_capture_review"
        db.flush()
        return
    if payment.status in {PaymentStatus.refunded, PaymentStatus.reversed}:
        purchase.status = (
            PrepaidPeriodPurchaseStatus.review_required
            if other_held is not None
            else PrepaidPeriodPurchaseStatus.canceled
        )
        purchase.failure_code = f"{_OWNER}.payment_reversed"
    else:
        purchase.status = PrepaidPeriodPurchaseStatus.review_required
        purchase.failure_code = f"{_OWNER}.payment_partially_refunded"
    db.flush()


@dataclass(frozen=True, slots=True)
class ResolveUnpaidPurchaseIntentCommand:
    intent_id: UUID


def unpaid_purchase_intent_can_close(
    db: Session, purchase: PrepaidPeriodPurchase
) -> bool:
    """One policy for admission, projections and terminal-observation recovery."""
    if (
        purchase.payment_id is not None
        or purchase.completed_at is not None
        or purchase.topup_intent_id is None
    ):
        return False
    if purchase.status not in {
        PrepaidPeriodPurchaseStatus.quoted,
        PrepaidPeriodPurchaseStatus.payment_pending,
    }:
        return False
    intent = db.get(TopupIntent, purchase.topup_intent_id)
    if (
        intent is None
        or intent.account_id != purchase.account_id
        or intent.purpose != "prepaid_period_purchase"
        or intent.completed_payment_id is not None
    ):
        return False
    if (
        intent.status,
        intent.gateway_last_outcome,
        intent.gateway_last_reason_code,
    ) not in {
        ("failed", "failed", "provider_reported_failed"),
        ("abandoned", "abandoned", "provider_reported_abandoned"),
    }:
        return False
    receipt = db.scalar(
        select(Payment.id)
        .where(Payment.reserved_for_purchase_id == purchase.id)
        .limit(1)
    )
    service = db.scalar(
        select(PrepaidPeriodPurchasePeriod.id)
        .where(
            PrepaidPeriodPurchasePeriod.purchase_id == purchase.id,
            PrepaidPeriodPurchasePeriod.invoice_id.is_not(None),
        )
        .limit(1)
    )
    return receipt is None and service is None


def stage_unpaid_purchase_intent_resolution(
    db: Session, command: ResolveUnpaidPurchaseIntentCommand
) -> bool:
    """Flush-only participant; the caller holds the canonical account lock."""
    purchase = db.scalar(
        select(PrepaidPeriodPurchase)
        .where(PrepaidPeriodPurchase.topup_intent_id == command.intent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if purchase is None or not unpaid_purchase_intent_can_close(db, purchase):
        return False
    purchase.status = PrepaidPeriodPurchaseStatus.failed
    purchase.failure_code = f"{_OWNER}.provider_confirmed_unpaid"
    db.flush()
    return True
