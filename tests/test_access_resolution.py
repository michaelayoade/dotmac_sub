from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.catalog import AccessState, BillingMode, SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.services import access_resolution
from app.services.access_resolution import resolve_customer_access


def _resolver_objects(
    *,
    subscriber_status=SubscriberStatus.active,
    subscription_status=SubscriptionStatus.active,
    billing_mode=BillingMode.postpaid,
    is_active=True,
    billing_enabled=True,
    captive_redirect_enabled=False,
):
    subscriber_id = uuid.uuid4()
    subscriber = SimpleNamespace(
        id=subscriber_id,
        status=subscriber_status,
        billing_mode=billing_mode,
        is_active=is_active,
        billing_enabled=billing_enabled,
        captive_redirect_enabled=captive_redirect_enabled,
    )
    subscription = SimpleNamespace(
        id=uuid.uuid4(),
        subscriber_id=subscriber_id,
        status=subscription_status,
        billing_mode=billing_mode,
        subscriber=subscriber,
    )
    return subscriber, subscription


def test_access_resolution_active_postpaid_is_billable_and_allowed():
    _subscriber, subscription = _resolver_objects()

    decision = resolve_customer_access(subscription)

    assert decision.is_active_customer_service is True
    assert decision.is_billable_account is True
    assert decision.is_postpaid_invoice_eligible is True
    assert decision.is_prepaid_enforcement_eligible is False
    assert decision.radius_access_state == AccessState.active
    assert decision.radius_allowed is True
    assert decision.radius_blocked is False
    assert decision.access_block_reason is None


def test_access_resolution_prepaid_uses_prepaid_enforcement_path():
    _subscriber, subscription = _resolver_objects(billing_mode=BillingMode.prepaid)

    decision = resolve_customer_access(subscription)

    assert decision.is_postpaid_invoice_eligible is False
    assert decision.is_prepaid_enforcement_eligible is True
    assert decision.billing_block_reason == "prepaid_not_postpaid_invoice_eligible"
    assert decision.radius_mode == "active"


def test_access_resolution_parent_hard_block_overrides_subscription():
    _subscriber, subscription = _resolver_objects(
        subscriber_status=SubscriberStatus.disabled
    )

    decision = resolve_customer_access(subscription)

    assert decision.is_active_customer_service is False
    assert decision.is_billable_account is False
    assert decision.radius_access_state == AccessState.suspended
    assert decision.radius_allowed is False
    assert decision.radius_blocked is True
    assert decision.access_block_reason == "subscriber_status_disabled"


def test_access_resolution_raw_captive_flag_is_not_authority():
    _subscriber, subscription = _resolver_objects(
        subscription_status=SubscriptionStatus.suspended,
        captive_redirect_enabled=True,
    )

    decision = resolve_customer_access(subscription)

    assert decision.radius_access_state == AccessState.suspended
    assert decision.radius_allowed is False
    assert decision.radius_blocked is True
    assert decision.radius_mode == "reject"


def test_access_resolution_accepts_canonical_captive_mode():
    from app.models.enforcement_lock import AccessRestrictionMode

    _subscriber, subscription = _resolver_objects(
        subscription_status=SubscriptionStatus.suspended,
        captive_redirect_enabled=False,
    )

    decision = resolve_customer_access(
        subscription,
        access_restriction_mode=AccessRestrictionMode.captive,
    )

    assert decision.radius_access_state == AccessState.captive
    assert decision.radius_allowed is True
    assert decision.radius_mode == "captive"


def test_invalid_prepaid_currency_raises_stable_domain_error(monkeypatch):
    monkeypatch.setattr(
        access_resolution.settings_spec,
        "resolve_value",
        lambda *_args: "not-a-currency",
    )

    with pytest.raises(access_resolution.AccessResolutionError) as captured:
        access_resolution.resolve_prepaid_enforcement_currency(MagicMock())

    assert captured.value.code == "financial.access_resolution.invalid_currency"


def test_prepaid_funding_decision_normalizes_currency():
    decision = access_resolution.PrepaidFundingDecision(
        account_id=str(uuid.uuid4()),
        available_balance=Decimal("10.00"),
        required_balance=Decimal("5.00"),
        currency="ngn",
    )

    assert decision.currency == "NGN"
    assert decision.funded is True
