"""Canonical current-service coverage evidence for prepaid access policy.

This owner answers whether a collectible prepaid subscription is funded or has
an explicit non-financial service grant at a point in time.  A projected
``next_billing_at`` date is diagnostic state only: without one of the evidence
rows below it is an unresolved projection and must never authorize restoration
or adverse enforcement.

Historical paid-invoice rows are repaired into ``ServiceEntitlement`` by the
canonical reconciliation owner. They are not read-time coverage evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import Subscription
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionStatus,
)


class PrepaidCoverageSource(StrEnum):
    funded_entitlement = "funded_entitlement"
    service_extension_grant = "service_extension_grant"


class PrepaidCoverageStatus(StrEnum):
    covered = "covered"
    uncovered_due = "uncovered_due"
    unresolved_projection = "unresolved_projection"


@dataclass(frozen=True, slots=True)
class PrepaidCoverageEvidence:
    source: PrepaidCoverageSource
    source_id: UUID
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class PrepaidServiceCoverageDecision:
    subscription_id: UUID
    account_id: UUID
    as_of: datetime
    status: PrepaidCoverageStatus
    evidence: PrepaidCoverageEvidence | None
    projected_paid_through: datetime | None

    @property
    def covered(self) -> bool:
        return self.status == PrepaidCoverageStatus.covered


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def resolve_prepaid_service_coverage(
    db: Session,
    subscriptions: list[Subscription],
    *,
    as_of: datetime | None = None,
) -> dict[UUID, PrepaidServiceCoverageDecision]:
    """Resolve exact current coverage for a bounded subscription cohort."""
    observed_at = _as_utc(as_of or datetime.now(UTC))
    if not subscriptions:
        return {}
    subscription_ids = [subscription.id for subscription in subscriptions]
    evidence: dict[UUID, PrepaidCoverageEvidence] = {}

    # Canonical funded-period evidence.  Deterministic ordering makes duplicate
    # active rows harmless to the read decision while reconciliation reports the
    # invariant violation separately.
    entitlements = db.scalars(
        select(ServiceEntitlement)
        .where(
            ServiceEntitlement.subscription_id.in_(subscription_ids),
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at <= observed_at,
            ServiceEntitlement.ends_at > observed_at,
        )
        .order_by(
            ServiceEntitlement.subscription_id,
            ServiceEntitlement.starts_at.desc(),
            ServiceEntitlement.id.desc(),
        )
    ).all()
    for entitlement in entitlements:
        evidence.setdefault(
            entitlement.subscription_id,
            PrepaidCoverageEvidence(
                source=PrepaidCoverageSource.funded_entitlement,
                source_id=entitlement.id,
                starts_at=entitlement.starts_at,
                ends_at=entitlement.ends_at,
            ),
        )

    # An applied service extension is an explicit access grant only for the
    # added interval.  It does not fabricate financial entitlement for the
    # original service period.
    extension_rows = db.execute(
        select(
            ServiceExtensionEntry.subscription_id,
            ServiceExtensionEntry.id,
            ServiceExtensionEntry.grant_starts_at,
            ServiceExtensionEntry.grant_ends_at,
        )
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtensionEntry.subscription_id.in_(subscription_ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_starts_at.isnot(None),
            ServiceExtensionEntry.grant_starts_at <= observed_at,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at > observed_at,
        )
        .order_by(
            ServiceExtensionEntry.subscription_id,
            ServiceExtensionEntry.grant_starts_at.desc(),
            ServiceExtensionEntry.id.desc(),
        )
    ).all()
    for extension_row in extension_rows:
        if extension_row.subscription_id in evidence:
            continue
        assert extension_row.grant_starts_at is not None
        assert extension_row.grant_ends_at is not None
        evidence[extension_row.subscription_id] = PrepaidCoverageEvidence(
            source=PrepaidCoverageSource.service_extension_grant,
            source_id=extension_row.id,
            starts_at=extension_row.grant_starts_at,
            ends_at=extension_row.grant_ends_at,
        )

    decisions: dict[UUID, PrepaidServiceCoverageDecision] = {}
    for subscription in subscriptions:
        current_evidence = evidence.get(subscription.id)
        paid_through = subscription.next_billing_at
        projected_future = bool(
            paid_through is not None and _as_utc(paid_through) > observed_at
        )
        status = (
            PrepaidCoverageStatus.covered
            if current_evidence is not None
            else (
                PrepaidCoverageStatus.unresolved_projection
                if projected_future
                else PrepaidCoverageStatus.uncovered_due
            )
        )
        decisions[subscription.id] = PrepaidServiceCoverageDecision(
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            as_of=observed_at,
            status=status,
            evidence=current_evidence,
            projected_paid_through=paid_through,
        )
    return decisions
