import uuid

import pytest

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.services.billing_profile import (
    BillingModeWriteRejected,
    BillingProfileError,
    BillingProfileReason,
    require_effective_billing_mode,
    resolve_billing_profile,
    resolve_billing_profiles,
    resolve_subscription_billing_mode_for_write,
)
from app.services.collections._core import (
    _account_has_prepaid_service,
    _effective_billing_mode_for_account,
)


def test_profile_uses_single_collectible_subscription_mode_when_account_drifts(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    profile = resolve_billing_profile(db_session, subscriber_account)

    assert profile.is_valid is True
    assert profile.effective_mode == BillingMode.prepaid
    assert profile.source == "subscription"
    assert profile.account_subscription_mismatch is True
    assert profile.automation_safe is False
    assert _effective_billing_mode_for_account(db_session, subscriber_account) == (
        BillingMode.prepaid
    )
    assert _account_has_prepaid_service(db_session, subscriber_account) is True


def test_profile_rejects_mixed_collectible_subscription_modes(
    db_session, subscriber_account, subscription, catalog_offer
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    db_session.add(
        Subscription(
            subscriber_id=subscriber_account.id,
            offer_id=catalog_offer.id,
            billing_mode=BillingMode.prepaid,
            status=SubscriptionStatus.active,
        )
    )
    db_session.commit()

    profile = resolve_billing_profile(db_session, subscriber_account)

    assert profile.is_valid is False
    assert profile.automation_safe is False
    assert profile.effective_mode is None
    assert profile.invalid_reason == "mixed_collectible_subscription_billing_modes"
    assert _effective_billing_mode_for_account(db_session, subscriber_account) is None
    assert _account_has_prepaid_service(db_session, subscriber_account) is False


def test_profile_falls_back_to_account_without_collectible_subscriptions(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.canceled
    db_session.commit()

    profile = resolve_billing_profile(db_session, subscriber_account)

    assert profile.is_valid is True
    assert profile.effective_mode == BillingMode.postpaid
    assert profile.source == "account"
    assert profile.account_subscription_mismatch is False


def test_profile_fails_closed_for_legacy_missing_account_mode(
    db_session, subscriber_account, subscription
):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    # Current schema makes this column NOT NULL. Keep the resolver defensive
    # for a legacy/drifted database row without weakening that DB invariant.
    subscriber_account.billing_mode = None
    with db_session.no_autoflush:
        profile = resolve_billing_profile(db_session, subscriber_account)

    assert profile.is_valid is False
    assert profile.automation_safe is False
    assert profile.effective_mode == BillingMode.prepaid
    assert profile.invalid_reason == "account_billing_mode_missing"


def test_batch_profiles_match_single_account_resolution(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    profiles = resolve_billing_profiles(db_session, [subscriber_account])

    assert profiles[subscriber_account.id] == resolve_billing_profile(
        db_session, subscriber_account
    )


def test_invalid_profile_fails_closed_with_stable_domain_error(
    db_session, subscriber_account, subscription, catalog_offer
):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.add(
        Subscription(
            subscriber_id=subscriber_account.id,
            offer_id=catalog_offer.id,
            billing_mode=BillingMode.postpaid,
            status=SubscriptionStatus.active,
        )
    )
    db_session.commit()

    profile = resolve_billing_profile(db_session, subscriber_account)

    with pytest.raises(BillingProfileError) as exc_info:
        require_effective_billing_mode(profile)

    assert exc_info.value.reason is (
        BillingProfileReason.MIXED_COLLECTIBLE_SUBSCRIPTION_BILLING_MODES
    )
    assert exc_info.value.code == (
        "financial.billing_profile.mixed_collectible_subscription_billing_modes"
    )


def test_subscription_write_failure_has_stable_domain_code(db_session):
    with pytest.raises(BillingModeWriteRejected) as exc_info:
        resolve_subscription_billing_mode_for_write(
            db_session,
            account_id=uuid.uuid4(),
            offer_id=uuid.uuid4(),
        )

    assert exc_info.value.reason is BillingProfileReason.SUBSCRIBER_NOT_FOUND
    assert exc_info.value.code == "financial.billing_profile.subscriber_not_found"
