"""Exact evidence, never a billing anchor, owns prepaid current coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.services.prepaid_service_coverage import (
    PrepaidCoverageSource,
    PrepaidCoverageStatus,
    resolve_prepaid_service_coverage,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = None
    db.commit()


def test_future_anchor_without_evidence_is_unresolved(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscription.next_billing_at = NOW + timedelta(days=12)
    db_session.commit()

    decision = resolve_prepaid_service_coverage(db_session, [subscription], as_of=NOW)[
        subscription.id
    ]

    assert decision.status == PrepaidCoverageStatus.unresolved_projection
    assert decision.evidence is None


def test_current_entitlement_is_primary_coverage(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    entitlement = ServiceEntitlement(
        account_id=subscriber_account.id,
        subscription_id=subscription.id,
        status=ServiceEntitlementStatus.active,
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW + timedelta(days=29),
        amount_funded=Decimal("35000.00"),
    )
    db_session.add(entitlement)
    db_session.commit()

    decision = resolve_prepaid_service_coverage(db_session, [subscription], as_of=NOW)[
        subscription.id
    ]

    assert decision.status == PrepaidCoverageStatus.covered
    assert decision.evidence is not None
    assert decision.evidence.source == PrepaidCoverageSource.funded_entitlement
    assert decision.evidence.source_id == entitlement.id


def test_applied_extension_covers_only_its_exact_granted_interval(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    extension = ServiceExtension(
        reason="verified outage compensation",
        window_start=NOW - timedelta(days=7),
        window_end=NOW - timedelta(days=6),
        days=3,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscriber_account.id)],
        status=ServiceExtensionStatus.applied,
        applied_at=NOW - timedelta(days=2),
    )
    db_session.add(extension)
    db_session.flush()
    entry = ServiceExtensionEntry(
        extension_id=extension.id,
        subscription_id=subscription.id,
        subscriber_id=subscriber_account.id,
        previous_next_billing_at=NOW - timedelta(days=1),
        new_next_billing_at=NOW + timedelta(days=2),
    )
    db_session.add(entry)
    db_session.commit()

    current = resolve_prepaid_service_coverage(db_session, [subscription], as_of=NOW)[
        subscription.id
    ]
    after_grant = resolve_prepaid_service_coverage(
        db_session, [subscription], as_of=NOW + timedelta(days=3)
    )[subscription.id]

    assert current.status == PrepaidCoverageStatus.covered
    assert current.evidence is not None
    assert current.evidence.source == PrepaidCoverageSource.service_extension_grant
    assert after_grant.status == PrepaidCoverageStatus.uncovered_due
