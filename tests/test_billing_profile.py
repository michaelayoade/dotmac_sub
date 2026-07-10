from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.services.billing_profile import resolve_billing_profile
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
