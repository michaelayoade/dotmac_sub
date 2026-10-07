"""Typed purchase coverage facts, independent of settlement orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.service_period_purchase import (
    OutageCompensationDecision,
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchasePeriod,
    PrepaidPeriodPurchaseStatus,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PurchasedCoverageQuery:
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class PurchasedCoverage:
    protected_until: datetime | None
    has_unsettled_purchase: bool


def resolve_purchased_coverage(
    db: Session, query: PurchasedCoverageQuery
) -> PurchasedCoverage:
    """Read paid, still-active purchased coverage and unresolved checkout facts."""
    purchased_rows = list(
        db.scalars(
            select(ServiceEntitlement)
            .join(
                PrepaidPeriodPurchasePeriod,
                PrepaidPeriodPurchasePeriod.entitlement_id == ServiceEntitlement.id,
            )
            .where(
                PrepaidPeriodPurchasePeriod.subscription_id == query.subscription_id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        ).all()
    )
    tail = max((_utc(row.ends_at) for row in purchased_rows), default=None)
    protected_ids = {str(row.id) for row in purchased_rows}
    grants = list(
        db.scalars(
            select(ServiceEntitlement).where(
                ServiceEntitlement.subscription_id == query.subscription_id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
                ServiceEntitlement.source_outage_compensation_id.is_not(None),
            )
        ).all()
    )
    while True:
        included = False
        for grant in grants:
            if str(grant.id) in protected_ids:
                continue
            decision = db.get(
                OutageCompensationDecision, grant.source_outage_compensation_id
            )
            if decision and protected_ids.intersection(
                (decision.policy_snapshot or {}).get("funded_entitlement_ids", [])
            ):
                protected_ids.add(str(grant.id))
                tail = max(tail, _utc(grant.ends_at)) if tail else _utc(grant.ends_at)
                included = True
        if not included:
            break
    pending = db.scalar(
        select(PrepaidPeriodPurchase)
        .where(
            PrepaidPeriodPurchase.subscription_id == query.subscription_id,
            PrepaidPeriodPurchase.status.in_(
                [
                    PrepaidPeriodPurchaseStatus.quoted,
                    PrepaidPeriodPurchaseStatus.payment_pending,
                    PrepaidPeriodPurchaseStatus.review_required,
                ]
            ),
            or_(
                PrepaidPeriodPurchase.status != PrepaidPeriodPurchaseStatus.quoted,
                PrepaidPeriodPurchase.expires_at > datetime.now(UTC),
                PrepaidPeriodPurchase.topup_intent_id.is_not(None),
                PrepaidPeriodPurchase.payment_id.is_not(None),
            ),
        )
        .limit(1)
    )
    from app.services.purchase_payment_recovery_state import (
        unpaid_purchase_intent_can_close,
    )

    unresolved = pending is not None and not unpaid_purchase_intent_can_close(
        db, pending
    )
    return PurchasedCoverage(_utc(tail) if tail else None, unresolved)
