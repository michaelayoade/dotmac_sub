import uuid
from pathlib import Path

import pytest

from app.models.billing import PaymentProvider, PaymentProviderType, TopupIntent
from app.services.payment_gateway_adapter import payment_gateway_adapter
from app.services.payment_routing import (
    gateway_options,
    provider_for_intent,
    provider_health,
    select_checkout_provider,
)
from tests.integration_platform_helpers import enable_payment_provider


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


def test_gateway_options_follow_binding_presentment_priority(db_session):
    paystack = _provider(db_session, PaymentProviderType.paystack)
    flutterwave = _provider(db_session, PaymentProviderType.flutterwave)
    enable_payment_provider(db_session, "paystack", presentment_priority=10)
    enable_payment_provider(db_session, "flutterwave", presentment_priority=20)

    routes = gateway_options(db_session)

    assert [(route.provider_type, route.provider_id) for route in routes] == [
        (PaymentProviderType.flutterwave, flutterwave.id),
        (PaymentProviderType.paystack, paystack.id),
    ]


def test_finance_active_flag_does_not_control_checkout(db_session):
    _provider(db_session, PaymentProviderType.paystack, active=False)
    enable_payment_provider(db_session, "paystack")

    assert [route.provider_type for route in gateway_options(db_session)] == [
        PaymentProviderType.paystack
    ]
    assert (
        select_checkout_provider(db_session, "paystack").provider_type
        == PaymentProviderType.paystack
    )


def test_default_selection_is_first_presentment_option(db_session):
    _provider(db_session, PaymentProviderType.paystack)
    _provider(db_session, PaymentProviderType.flutterwave)
    enable_payment_provider(db_session, "paystack", presentment_priority=5)
    enable_payment_provider(db_session, "flutterwave", presentment_priority=50)

    assert [route.provider_type for route in gateway_options(db_session)] == [
        PaymentProviderType.flutterwave,
        PaymentProviderType.paystack,
    ]
    assert (
        select_checkout_provider(db_session, "flutterwave").provider_type
        == PaymentProviderType.flutterwave
    )

    assert (
        select_checkout_provider(db_session).provider_type
        == PaymentProviderType.flutterwave
    )


def test_multiple_active_rows_make_provider_ambiguous(db_session):
    _provider(db_session, PaymentProviderType.paystack)
    _provider(db_session, PaymentProviderType.paystack)
    enable_payment_provider(db_session, "paystack")

    health = {row.provider_type: row for row in provider_health(db_session)}

    assert health[PaymentProviderType.paystack].health == "ambiguous"
    assert gateway_options(db_session) == []


def test_manifest_pin_drift_makes_gateway_unavailable(db_session):
    _provider(db_session, PaymentProviderType.paystack)
    bindings = enable_payment_provider(db_session, "paystack")
    installation = bindings["payments.intent.v1"].installation
    installation.manifest_digest = "0" * 64
    db_session.flush()

    health = {row.provider_type: row for row in provider_health(db_session)}

    paystack = health[PaymentProviderType.paystack]
    assert paystack.health == "misconfigured"
    assert paystack.health_label == "Installation definition changed"
    assert paystack.readiness_errors == ("definition_mismatch",)
    assert not paystack.capability_ready
    assert gateway_options(db_session) == []


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


def test_gateway_adapter_rejects_unknown_provider_before_fallback(db_session):
    with pytest.raises(ValueError, match="Unsupported payment provider"):
        payment_gateway_adapter.build_context(
            db_session,
            provider_type="stripe",
            capability_binding_id=uuid.UUID(int=0),
        )
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
