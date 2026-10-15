"""Logic canaries use the released immutable observation contracts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_payments import (
    PaymentIntentObservationV1,
    PaymentIntentStatus,
    PaymentPurpose,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.services import payment_intent_module_shadow as shadow
from app.services.operator_tenant import OPERATOR_TENANT_ID


@pytest.fixture
def binding() -> shadow.AuthorizedInvoiceIntentBinding:
    return shadow.AuthorizedInvoiceIntentBinding(
        reference="original-checkout",
        account_id=UUID("00000000-0000-0000-0000-000000000001"),
        invoice_reference=None,
        requested_amount=Decimal("100.01"),
        currency="NGN",
        provider_type="paystack",
        channel="customer_selfcare",
        opened_at=datetime(2020, 1, 1, tzinfo=UTC),
        expires_at=datetime(2020, 1, 2, tzinfo=UTC),
    )


def observation(
    binding: shadow.AuthorizedInvoiceIntentBinding,
) -> PaymentIntentObservationV1:
    return PaymentIntentObservationV1(
        scope=TenantScope(OPERATOR_TENANT_ID),
        intent_id=uuid4(),
        reference=binding.reference,
        payer_reference=str(binding.account_id),
        target_reference=binding.invoice_reference,
        purpose=PaymentPurpose.INVOICE_SETTLEMENT,
        requested=Money(binding.requested_amount, Currency(binding.currency, 2)),
        confirmed=None,
        provider_type=" PAYSTACK ",
        channel=" CUSTOMER_SELFCARE ",
        status=PaymentIntentStatus.PENDING,
        opened_at=binding.opened_at,
        expires_at=binding.expires_at,
        settled_at=None,
    )


def test_match_uses_same_session_scope_and_original_expiry(
    monkeypatch: pytest.MonkeyPatch, binding: shadow.AuthorizedInvoiceIntentBinding
) -> None:
    getter = Mock(return_value=observation(binding))
    monkeypatch.setattr(shadow, "get_payment_intent_v1", getter)
    db = Mock(spec=Session)
    result = shadow.observe_invoice_intent_parity(db, binding)
    assert result == shadow.IntentBindingParity(shadow.IntentParityOutcome.MATCH)
    args, kwargs = getter.call_args
    assert args == (db,)
    assert kwargs["scope"] == TenantScope(OPERATOR_TENANT_ID)
    assert kwargs["query"].reference == binding.reference


@pytest.mark.parametrize("field", tuple(shadow.IntentBindingField))
def test_each_frozen_binding_field_is_compared(
    monkeypatch: pytest.MonkeyPatch,
    binding: shadow.AuthorizedInvoiceIntentBinding,
    field: shadow.IntentBindingField,
) -> None:
    observed = observation(binding)
    match field:
        case shadow.IntentBindingField.PAYER:
            observed = replace(observed, payer_reference=str(uuid4()))
        case shadow.IntentBindingField.TARGET:
            observed = replace(observed, target_reference=str(uuid4()))
        case shadow.IntentBindingField.PURPOSE:
            observed = replace(observed, purpose=PaymentPurpose.ACCOUNT_CREDIT_DEPOSIT)
        case shadow.IntentBindingField.AMOUNT:
            observed = replace(
                observed, requested=Money(Decimal("100.02"), Currency("NGN", 2))
            )
        case shadow.IntentBindingField.CURRENCY:
            observed = replace(
                observed, requested=Money(Decimal("100.01"), Currency("USD", 2))
            )
        case shadow.IntentBindingField.SCALE:
            observed = replace(
                observed, requested=Money(Decimal("100.01"), Currency("NGN", 3))
            )
        case shadow.IntentBindingField.PROVIDER:
            observed = replace(observed, provider_type="flutterwave")
        case shadow.IntentBindingField.CHANNEL:
            observed = replace(observed, channel="reseller_selfcare")
        case shadow.IntentBindingField.OPENED_AT:
            observed = replace(
                observed, opened_at=binding.opened_at + timedelta(seconds=1)
            )
        case shadow.IntentBindingField.EXPIRES_AT:
            observed = replace(observed, expires_at=None)
    monkeypatch.setattr(shadow, "get_payment_intent_v1", Mock(return_value=observed))
    result = shadow.observe_invoice_intent_parity(Mock(spec=Session), binding)
    assert result == shadow.IntentBindingParity(
        shadow.IntentParityOutcome.MISMATCH, (field,)
    )


def test_missing_row_is_gap_and_database_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, binding: shadow.AuthorizedInvoiceIntentBinding
) -> None:
    monkeypatch.setattr(shadow, "get_payment_intent_v1", Mock(return_value=None))
    assert (
        shadow.observe_invoice_intent_parity(Mock(spec=Session), binding).outcome
        is shadow.IntentParityOutcome.MISSING
    )
    error = DBAPIError("query", {}, RuntimeError("database unavailable"))
    monkeypatch.setattr(shadow, "get_payment_intent_v1", Mock(side_effect=error))
    with pytest.raises(DBAPIError) as caught:
        shadow.observe_invoice_intent_parity(Mock(spec=Session), binding)
    assert caught.value is error


def test_lifecycle_and_confirmed_amount_are_not_settlement_equivalence(
    monkeypatch: pytest.MonkeyPatch, binding: shadow.AuthorizedInvoiceIntentBinding
) -> None:
    observed = replace(
        observation(binding),
        status=PaymentIntentStatus.CONFIRMED,
        confirmed=Money(Decimal("999.99"), Currency("NGN", 2)),
    )
    monkeypatch.setattr(shadow, "get_payment_intent_v1", Mock(return_value=observed))
    assert (
        shadow.observe_invoice_intent_parity(Mock(spec=Session), binding).outcome
        is shadow.IntentParityOutcome.MATCH
    )


def test_exact_amount_is_never_rounded_into_a_match(
    monkeypatch: pytest.MonkeyPatch, binding: shadow.AuthorizedInvoiceIntentBinding
) -> None:
    monkeypatch.setattr(
        shadow, "get_payment_intent_v1", Mock(return_value=observation(binding))
    )
    result = shadow.observe_invoice_intent_parity(
        Mock(spec=Session), replace(binding, requested_amount=Decimal("100.010001"))
    )
    assert result.mismatched_fields == (shadow.IntentBindingField.AMOUNT,)


def test_legacy_adapter_retains_missing_target_and_original_instants(
    binding: shadow.AuthorizedInvoiceIntentBinding,
) -> None:
    from app.models.billing import TopupIntent

    intent = TopupIntent(
        reference=binding.reference,
        account_id=binding.account_id,
        requested_amount=binding.requested_amount,
        currency=binding.currency,
        provider_type=binding.provider_type,
        channel=binding.channel,
        created_at=binding.opened_at,
        expires_at=binding.expires_at,
        metadata_={"payment_flow": "invoice_payment"},
    )
    assert shadow.authorized_invoice_binding(intent) == binding


def test_metric_labels_contain_only_closed_outcome_and_field_names(
    monkeypatch: pytest.MonkeyPatch,
    binding: shadow.AuthorizedInvoiceIntentBinding,
) -> None:
    metric = Mock()
    monkeypatch.setattr(shadow, "PAYMENT_INTENT_MODULE_PARITY", metric)
    monkeypatch.setattr(
        shadow,
        "get_payment_intent_v1",
        Mock(return_value=replace(observation(binding), target_reference=str(uuid4()))),
    )
    shadow.observe_invoice_intent_parity(Mock(spec=Session), binding)
    assert metric.labels.call_count == 2
    assert metric.labels.call_args_list[0].kwargs == {
        "outcome": "mismatch",
        "field": "none",
    }
    assert metric.labels.call_args_list[1].kwargs == {
        "outcome": "mismatch",
        "field": "target",
    }
