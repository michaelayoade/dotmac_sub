"""Typed Finance projections; every action remains a financial owner command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.service_period_purchase import (
    OutageCompensationDecision,
    OutageCompensationDecisionStatus,
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchaseStatus,
)
from app.services.domain_errors import DomainError
from app.services.outage_compensation import preview_outage_compensation
from app.services.prepaid_period_purchases import preview_purchase_recovery


class PeriodReviewKind(StrEnum):
    purchase = "purchase"
    outage = "outage"


@dataclass(frozen=True, slots=True)
class PeriodReviewQuery:
    limit: int = 50


@dataclass(frozen=True, slots=True)
class ReceiptReview:
    payment_id: UUID
    amount: Decimal
    refunded: Decimal
    currency: str
    status: str
    held_amount: Decimal


@dataclass(frozen=True, slots=True)
class PeriodReview:
    kind: PeriodReviewKind
    entity_id: UUID
    subscription_id: UUID
    account_id: UUID
    status: str
    reason: str
    action: str
    fingerprint: str | None
    created_at: datetime
    reference: str = ""
    receipts: tuple[ReceiptReview, ...] = ()
    seconds: int = 0
    tail_after: datetime | None = None


def resolve_period_reviews(
    db: Session, query: PeriodReviewQuery
) -> tuple[PeriodReview, ...]:
    limit = min(100, max(1, query.limit))
    from app.models.billing import Payment, TopupIntent
    from app.services.billing.payments import _payment_unallocated_credit_remaining

    rows: list[PeriodReview] = []
    purchases = db.scalars(
        select(PrepaidPeriodPurchase)
        .where(
            PrepaidPeriodPurchase.status.in_(
                [
                    PrepaidPeriodPurchaseStatus.payment_pending,
                    PrepaidPeriodPurchaseStatus.review_required,
                ]
            )
        )
        .order_by(PrepaidPeriodPurchase.created_at, PrepaidPeriodPurchase.id)
        .limit(limit)
    ).all()
    for purchase in purchases:
        preview = preview_purchase_recovery(db, purchase.id)
        intent = (
            db.get(TopupIntent, purchase.topup_intent_id)
            if purchase.topup_intent_id
            else None
        )
        held_receipts = {
            receipt.id: max(
                Decimal("0.00"),
                _payment_unallocated_credit_remaining(db, receipt)
                - receipt.refunded_amount,
            )
            for receipt in db.scalars(
                select(Payment).where(Payment.reserved_for_purchase_id == purchase.id)
            ).all()
        }
        rows.append(
            PeriodReview(
                kind=PeriodReviewKind.purchase,
                entity_id=purchase.id,
                subscription_id=purchase.subscription_id,
                account_id=purchase.account_id,
                status=purchase.status.value,
                reason=preview.failure_code or "Provider or billing review pending",
                action=preview.action.value,
                fingerprint=preview.fingerprint,
                created_at=purchase.created_at,
                reference=intent.reference if intent else str(purchase.id),
                receipts=tuple(
                    ReceiptReview(
                        item.payment_id,
                        item.amount,
                        item.refunded_amount,
                        item.currency,
                        item.status.value,
                        held_receipts.get(item.payment_id, Decimal("0.00")),
                    )
                    for item in preview.receipts
                ),
            )
        )
    decisions = db.scalars(
        select(OutageCompensationDecision)
        .where(
            OutageCompensationDecision.status.in_(
                [
                    OutageCompensationDecisionStatus.awaiting_approval,
                    OutageCompensationDecisionStatus.review_required,
                ]
            ),
            OutageCompensationDecision.resolved_by_decision_id.is_(None),
        )
        .order_by(OutageCompensationDecision.created_at, OutageCompensationDecision.id)
        .limit(limit)
    ).all()
    for decision in decisions:
        fingerprint = None
        action = "review_evidence"
        reason = "Review downtime, funding and previous compensation"
        seconds = decision.funded_overlap_seconds
        tail = decision.tail_after
        try:
            outage_preview = preview_outage_compensation(
                db,
                subscription_id=decision.subscription_id,
                effective_at=datetime.now(UTC),
                review_decision_id=decision.id,
            )
            fingerprint = outage_preview.fingerprint
            seconds, tail = (
                outage_preview.funded_overlap_seconds,
                outage_preview.tail_after,
            )
            if outage_preview.status is OutageCompensationDecisionStatus.compensated:
                action, reason = (
                    "approve",
                    "Exact compensation requires separate staff approval",
                )
            elif outage_preview.policy_snapshot.get("unresolved_time_credit_ids"):
                reason = (
                    "Attest historical time credit before approving another remedy: "
                    + ", ".join(
                        outage_preview.policy_snapshot["unresolved_time_credit_ids"]
                    )
                )
        except DomainError as exc:
            reason = exc.message
        rows.append(
            PeriodReview(
                kind=PeriodReviewKind.outage,
                entity_id=decision.id,
                subscription_id=decision.subscription_id,
                account_id=decision.account_id,
                status=decision.status.value,
                reason=reason,
                action=action,
                fingerprint=fingerprint,
                created_at=decision.created_at,
                seconds=seconds,
                tail_after=tail,
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.created_at.replace(tzinfo=UTC)
                if row.created_at.tzinfo is None
                else row.created_at
            ),
        )
    )
