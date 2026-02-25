from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services.web_billing_providers import (
    build_gateway_reconciliation,
    get_failover_state,
    list_data,
    parse_supported_provider_type,
    run_provider_test,
    supported_provider_type_values,
    update_failover_config,
)


def test_supported_provider_types_only_paystack_flutterwave():
    assert supported_provider_type_values() == ["paystack", "flutterwave"]


def test_parse_supported_provider_type_rejects_non_supported():
    with pytest.raises(ValueError):
        parse_supported_provider_type("stripe")


def test_run_provider_test_paystack_success(db_session):
    provider = PaymentProvider(
        name="Paystack Primary",
        provider_type=PaymentProviderType.paystack,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()

    domain_settings_service.billing_settings.upsert_by_key(
        db_session,
        "paystack_secret_key",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="sk_test_abc123"),
    )
    domain_settings_service.billing_settings.upsert_by_key(
        db_session,
        "paystack_public_key",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="pk_test_abc123"),
    )

    result = run_provider_test(db_session, provider_type_value="paystack", mode="test")

    assert result["ok"] is True
    assert result["errors"] == []


def test_update_failover_config_persists_state(db_session):
    update_failover_config(
        db_session,
        failover_enabled=False,
        primary_provider="flutterwave",
        secondary_provider="paystack",
    )

    state = get_failover_state(db_session)
    assert state["enabled"] is False
    assert state["primary"] == "flutterwave"
    assert state["secondary"] == "paystack"


def test_build_gateway_reconciliation_counts(db_session, subscriber):
    provider = PaymentProvider(
        name="Paystack Main",
        provider_type=PaymentProviderType.paystack,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)

    payment_matched = Payment(
        account_id=subscriber.id,
        provider_id=provider.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id="ref-1",
    )
    payment_missing_ref = Payment(
        account_id=subscriber.id,
        provider_id=provider.id,
        amount=Decimal("40.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=None,
    )
    db_session.add_all([payment_matched, payment_missing_ref])
    db_session.commit()

    event_matched = PaymentProviderEvent(
        provider_id=provider.id,
        payment_id=payment_matched.id,
        event_type="charge.succeeded",
        external_id="ref-1",
        status=PaymentProviderEventStatus.processed,
    )
    event_extra = PaymentProviderEvent(
        provider_id=provider.id,
        event_type="charge.succeeded",
        external_id="ref-2",
        status=PaymentProviderEventStatus.processed,
    )
    db_session.add_all([event_matched, event_extra])
    db_session.commit()

    payload = build_gateway_reconciliation(db_session)
    paystack_row = [row for row in payload["rows"] if row["provider_type"] == "paystack"][0]

    assert paystack_row["payment_count"] == 2
    assert paystack_row["event_count"] == 2
    assert paystack_row["matched_count"] == 1
    assert paystack_row["missing_in_gateway"] == 0
    assert paystack_row["missing_in_dotmac"] == 1
    assert paystack_row["payments_missing_reference"] == 1


def test_list_data_filters_non_supported_provider_types(db_session):
    db_session.add(
        PaymentProvider(
            name="Stripe Legacy",
            provider_type=PaymentProviderType.stripe,
            is_active=True,
        )
    )
    db_session.add(
        PaymentProvider(
            name="Flutterwave Main",
            provider_type=PaymentProviderType.flutterwave,
            is_active=True,
        )
    )
    db_session.commit()

    payload = list_data(db_session, show_inactive=False)
    provider_types = {item.provider_type.value for item in payload["providers"]}
    assert provider_types == {"flutterwave"}
