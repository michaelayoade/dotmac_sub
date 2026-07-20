import uuid
from pathlib import Path

import pytest

from app.models.billing import PaymentProvider, PaymentProviderType, TopupIntent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.payment_gateway_adapter import payment_gateway_adapter
from app.services.payment_routing import (
    backfill_legacy_provider_routes,
    eligible_routes,
    provider_for_intent,
    provider_health,
    select_checkout_provider,
)
from app.services.settings_cache import SettingsCache


def _setting(db, key: str, value: str | bool) -> None:
    setting = (
        db.query(DomainSetting).filter_by(domain=SettingDomain.billing, key=key).first()
    )
    if setting is None:
        setting = DomainSetting(domain=SettingDomain.billing, key=key)
    setting.value_type = (
        SettingValueType.boolean if isinstance(value, bool) else SettingValueType.string
    )
    setting.value_text = str(value).lower() if isinstance(value, bool) else value
    setting.value_json = None
    setting.is_secret = "secret" in key
    setting.is_active = True
    db.add(setting)
    db.commit()
    SettingsCache.invalidate(SettingDomain.billing.value, key)


def _provider(db, provider_type: PaymentProviderType, *, active: bool = True):
    provider = PaymentProvider(
        name=f"{provider_type.value}-{uuid.uuid4().hex}",
        provider_type=provider_type,
        is_active=active,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _credentials(db, provider_type: PaymentProviderType) -> None:
    if provider_type == PaymentProviderType.paystack:
        _setting(db, "paystack_secret_key", "sk_test_route")
        _setting(db, "paystack_public_key", "pk_test_route")
        return
    _setting(db, "flutterwave_secret_key", "FLWSECK_TEST-route")
    _setting(db, "flutterwave_public_key", "FLWPUBK_TEST-route")
    _setting(db, "flutterwave_secret_hash", "route-hash")


def test_routes_only_healthy_providers_in_policy_order(db_session):
    paystack = _provider(db_session, PaymentProviderType.paystack)
    flutterwave = _provider(db_session, PaymentProviderType.flutterwave)
    _credentials(db_session, PaymentProviderType.paystack)
    _credentials(db_session, PaymentProviderType.flutterwave)
    _setting(db_session, "payment_gateway_primary_provider", "flutterwave")
    _setting(db_session, "payment_gateway_secondary_provider", "paystack")

    routes = eligible_routes(db_session)

    assert [(route.provider_type, route.provider_id) for route in routes] == [
        (PaymentProviderType.flutterwave, str(flutterwave.id)),
        (PaymentProviderType.paystack, str(paystack.id)),
    ]


def test_disabled_provider_is_not_available_for_new_checkout(db_session):
    _provider(db_session, PaymentProviderType.paystack, active=False)
    _credentials(db_session, PaymentProviderType.paystack)

    assert eligible_routes(db_session) == []
    with pytest.raises(ValueError, match="not available"):
        select_checkout_provider(db_session, "paystack")


def test_failover_disabled_prevents_automatic_fallback_but_keeps_manual_choice(
    db_session,
):
    _provider(db_session, PaymentProviderType.paystack)
    _provider(db_session, PaymentProviderType.flutterwave)
    _credentials(db_session, PaymentProviderType.paystack)
    _credentials(db_session, PaymentProviderType.flutterwave)
    _setting(db_session, "payment_gateway_failover_enabled", False)

    assert [route.provider_type for route in eligible_routes(db_session)] == [
        PaymentProviderType.paystack,
        PaymentProviderType.flutterwave,
    ]
    assert (
        select_checkout_provider(db_session, "flutterwave").provider_type
        == PaymentProviderType.flutterwave
    )

    paystack = (
        db_session.query(PaymentProvider)
        .filter_by(provider_type=PaymentProviderType.paystack)
        .one()
    )
    paystack.is_active = False
    db_session.commit()
    with pytest.raises(ValueError, match="No online"):
        select_checkout_provider(db_session)


def test_multiple_active_rows_make_provider_ambiguous(db_session):
    _provider(db_session, PaymentProviderType.paystack)
    _provider(db_session, PaymentProviderType.paystack)
    _credentials(db_session, PaymentProviderType.paystack)

    health = {row["provider_type"]: row for row in provider_health(db_session)}

    assert health["paystack"]["health"] == "ambiguous"
    assert eligible_routes(db_session) == []


def test_intent_provider_is_authoritative_after_checkout(db_session, subscriber):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="intent-provider-sot",
        provider_type="paystack",
        requested_amount=100,
    )

    assert provider_for_intent(intent).value == "paystack"
    with pytest.raises(ValueError, match="does not match"):
        provider_for_intent(intent, "flutterwave")


def test_backfill_materializes_legacy_implicit_provider(db_session):
    _credentials(db_session, PaymentProviderType.paystack)

    created = backfill_legacy_provider_routes(db_session)

    assert created == [PaymentProviderType.paystack]
    provider = db_session.query(PaymentProvider).one()
    assert provider.provider_type == PaymentProviderType.paystack
    assert provider.is_active is True


def test_backfill_never_reactivates_disabled_provider(db_session):
    provider = _provider(db_session, PaymentProviderType.paystack, active=False)
    _credentials(db_session, PaymentProviderType.paystack)

    assert backfill_legacy_provider_routes(db_session) == []
    db_session.refresh(provider)
    assert provider.is_active is False


def test_gateway_adapter_rejects_unknown_provider_before_fallback(db_session):
    with pytest.raises(ValueError, match="Unsupported payment provider"):
        payment_gateway_adapter.build_context(db_session, provider_type="stripe")
    with pytest.raises(ValueError, match="Unsupported payment provider"):
        payment_gateway_adapter.verify(
            db_session, provider_type="stripe", reference="unknown"
        )


def test_legacy_default_provider_has_no_runtime_consumer():
    app_root = Path(__file__).resolve().parents[1] / "app"
    runtime_paths = [
        app_root / "services/customer_portal_flow_payments.py",
        app_root / "services/reseller_portal_billing.py",
        app_root / "services/billing/providers.py",
    ]
    consumers = []
    for path in runtime_paths:
        if "default_payment_provider_type" in path.read_text():
            consumers.append(str(path.relative_to(app_root)))
    assert consumers == []
