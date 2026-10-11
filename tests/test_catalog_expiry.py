from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionAnchorBasis,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.services import catalog as catalog_service

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _prepaid_due(subscription) -> None:
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.end_at = None
    subscription.next_billing_at = NOW - timedelta(days=1)


def test_expire_subscriptions_keeps_uncovered_prepaid_service_nonterminal(
    db_session, subscription
):
    _prepaid_due(subscription)
    db_session.commit()

    result = catalog_service.subscriptions.expire_subscriptions(
        db_session,
        run_at=NOW,
    )

    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert result["subscriptions_matched"] == 0
    assert result["subscriptions_expired"] == 0
    assert result["subscriptions_coverage_protected"] == 0
    assert result["subscriptions_coverage_unresolved"] == 0


def test_expire_subscriptions_keeps_suspended_prepaid_service_nonterminal(
    db_session, subscriber_account, subscription
):
    _prepaid_due(subscription)
    subscription.status = SubscriptionStatus.suspended
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber_account.id,
        reason=EnforcementReason.prepaid,
        source="test:prepaid_due",
    )
    db_session.add(lock)
    db_session.commit()

    result = catalog_service.subscriptions.expire_subscriptions(
        db_session,
        run_at=NOW,
    )

    db_session.refresh(subscription)
    db_session.refresh(lock)
    assert subscription.status == SubscriptionStatus.suspended
    assert lock.is_active is True
    assert result["subscriptions_matched"] == 0
    assert result["subscriptions_expired"] == 0


def test_expire_subscriptions_expires_uncovered_fixed_term_prepaid_service(
    db_session, subscription
):
    _prepaid_due(subscription)
    subscription.end_at = NOW - timedelta(days=1)
    db_session.commit()

    result = catalog_service.subscriptions.expire_subscriptions(
        db_session,
        run_at=NOW,
    )

    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.expired
    assert result["subscriptions_matched"] == 1
    assert result["subscriptions_expired"] == 1


def test_expire_subscriptions_keeps_prepaid_service_with_paid_coverage(
    db_session, subscriber_account, subscription
):
    _prepaid_due(subscription)
    subscription.end_at = NOW - timedelta(days=1)
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            status=ServiceEntitlementStatus.active,
            starts_at=NOW - timedelta(days=2),
            ends_at=NOW + timedelta(days=28),
            amount_funded=Decimal("35000.00"),
        )
    )
    db_session.commit()

    result = catalog_service.subscriptions.expire_subscriptions(
        db_session,
        run_at=NOW,
    )

    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert result["subscriptions_expired"] == 0
    assert result["subscriptions_coverage_protected"] == 1


def test_expire_subscriptions_keeps_prepaid_service_with_outage_extension(
    db_session, subscriber_account, subscription
):
    _prepaid_due(subscription)
    subscription.end_at = NOW - timedelta(days=1)
    extension = ServiceExtension(
        reason="verified outage compensation",
        window_start=NOW - timedelta(days=3),
        window_end=NOW - timedelta(days=2),
        days=2,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscriber_account.id)],
        status=ServiceExtensionStatus.applied,
        applied_at=NOW - timedelta(days=1),
    )
    db_session.add(extension)
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscription_id=subscription.id,
            subscriber_id=subscriber_account.id,
            previous_next_billing_at=NOW - timedelta(days=1),
            grant_starts_at=NOW - timedelta(days=1),
            grant_ends_at=NOW + timedelta(days=1),
            anchor_basis=ServiceExtensionAnchorBasis.legacy_previous_anchor,
            new_next_billing_at=NOW + timedelta(days=1),
        )
    )
    db_session.commit()

    result = catalog_service.subscriptions.expire_subscriptions(
        db_session,
        run_at=NOW,
    )

    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert result["subscriptions_expired"] == 0
    assert result["subscriptions_coverage_protected"] == 1


def test_expire_subscriptions_holds_future_unresolved_projection(
    db_session, subscription
):
    _prepaid_due(subscription)
    subscription.end_at = NOW - timedelta(days=1)
    subscription.next_billing_at = NOW + timedelta(days=5)
    db_session.commit()

    result = catalog_service.subscriptions.expire_subscriptions(
        db_session,
        run_at=NOW,
    )

    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert result["subscriptions_expired"] == 0
    assert result["subscriptions_coverage_unresolved"] == 1
