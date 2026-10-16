from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.services.integrations import payment_capability
from app.services.integrations.connectors.payment_gateway import (
    PAYMENT_INTENT_CAPABILITY,
    PAYMENT_RECONCILE_CAPABILITY,
    PaymentGatewayRunner,
)
from app.services.integrations.registry import require_connector_definition
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
)
from tests.integration_platform_helpers import enable_payment_provider


class _Client:
    def __init__(self, body: dict):
        self.body = body
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            json=self.body,
        )


def _envelope(
    action: str,
    params: dict,
    *,
    capability_id: str = PAYMENT_INTENT_CAPABILITY,
) -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=uuid4(),
        installation_id=uuid4(),
        config_revision_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=capability_id,
        connector_key="paystack",
        connector_version="1.0.0",
        manifest_digest="a" * 64,
        trigger=OperationTrigger.interactive,
        correlation_id="payment-test",
        idempotency_key="payment-test-operation",
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        payload={"action": action, "params": params},
    )


def test_paystack_runner_uses_installation_config_and_materialized_secret():
    client = _Client(
        {
            "status": True,
            "data": {"authorization_url": "https://pay.example.test/checkout"},
        }
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)

    result = runner.execute(
        _envelope(
            "initialize",
            {
                "email": "customer@example.test",
                "amount_kobo": 500000,
                "reference": "DMAC-TEST-1",
                "redirect_url": "https://sub.example.test/verify",
                "metadata": {"invoice_id": "invoice-1"},
            },
        ),
        config={"base_url": "https://gateway.example.test", "timeout_seconds": 9},
        secret_material={"gateway_credentials": "materialized-test-secret"},
    )

    assert result.status == OperationStatus.succeeded
    assert result.output["item"]["authorization_url"].endswith("/checkout")
    call = client.calls[0]
    assert call["url"] == "https://gateway.example.test/transaction/initialize"
    assert call["headers"] == {"Authorization": "Bearer materialized-test-secret"}
    assert call["timeout"] == 9
    assert call["json"]["metadata"] == {"invoice_id": "invoice-1"}


def test_flutterwave_runner_applies_installation_default_currency():
    client = _Client(
        {"status": "success", "data": {"link": "https://flw.example.test/pay"}}
    )
    runner = PaymentGatewayRunner("flutterwave", client_override=client)

    result = runner.execute(
        _envelope(
            "initialize",
            {
                "email": "customer@example.test",
                "amount": "100.00",
                "reference": "FLW-TEST-1",
                "redirect_url": "https://sub.example.test/verify",
            },
        ),
        config={
            "base_url": "https://flw.example.test",
            "timeout_seconds": 5,
            "default_currency": "GHS",
        },
        secret_material={"gateway_credentials": "materialized-test-secret"},
    )

    assert result.status == OperationStatus.succeeded
    assert client.calls[0]["json"]["currency"] == "GHS"


def test_payment_runner_rejects_actions_outside_capability_allow_list():
    runner = PaymentGatewayRunner("paystack", client_override=_Client({}))

    result = runner.execute(
        _envelope("refund", {"transaction_id": "tx-1"}),
        config={"base_url": "https://gateway.example.test"},
        secret_material={"gateway_credentials": "materialized-test-secret"},
    )

    assert result.status == OperationStatus.rejected
    assert result.error_code == "operation_not_allowed"


def test_paystack_reconciliation_lists_one_bounded_transaction_page():
    client = _Client(
        {
            "status": True,
            "data": [{"id": 1001, "reference": "ref-1"}],
            "meta": {"page": 1, "pageCount": 2},
        }
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)

    result = runner.execute(
        _envelope(
            "list_transactions",
            {
                "from_date": "2026-06-15",
                "to_date": "2026-06-18",
                "status": "success",
                "page": 1,
                "per_page": 100,
            },
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
        ),
        config={"base_url": "https://gateway.example.test", "timeout_seconds": 9},
        secret_material={"gateway_credentials": "materialized-test-secret"},
    )

    assert result.status == OperationStatus.succeeded
    assert result.output["items"] == [{"id": 1001, "reference": "ref-1"}]
    assert result.output["meta"] == {"page": 1, "pageCount": 2}
    assert client.calls[0]["params"]["status"] == "success"


def test_payment_manifests_use_builtin_runtime_and_no_settings_credentials():
    for connector_key in ("paystack", "flutterwave"):
        manifest = require_connector_definition(connector_key)
        assert manifest.runtime.type.value == "builtin_worker"
        assert manifest.capability("payments.webhook.v1") is not None
        assert {secret.name for secret in manifest.secrets} >= {
            "gateway_credentials",
            "public_key",
        }
        assert manifest.config_schema["properties"]["base_url"]["default"].startswith(
            "https://api."
        )
        assert manifest.config_schema["properties"]["timeout_seconds"]["default"] == 30


def test_pinned_checkout_source_must_be_payment_intent_binding(db_session):
    bindings = enable_payment_provider(db_session, "paystack")

    with pytest.raises(
        payment_capability.PaymentCapabilityError,
        match="not a payment intent binding",
    ):
        payment_capability._binding(
            db_session,
            "paystack",
            PAYMENT_RECONCILE_CAPABILITY,
            checkout_binding_id=bindings["payments.webhook.v1"].id,
        )


def test_saved_authorization_charge_sends_explicit_currency_without_default():
    client = _Client(
        {"status": True, "data": {"reference": "currency-ref", "status": "success"}}
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)
    result = runner.execute(
        _envelope(
            "charge_authorization",
            {
                "authorization_code": "test-authorization",
                "email": "payer@example.test",
                "amount_kobo": 250001,
                "currency": "USD",
                "reference": "currency-ref",
                "metadata": {},
            },
        ),
        config={"base_url": "https://gateway.example.test", "default_currency": "NGN"},
        secret_material={"gateway_credentials": "materialized-test-secret"},
    )
    assert result.status == OperationStatus.succeeded
    assert client.calls[0]["json"]["amount"] == 250001
    assert client.calls[0]["json"]["currency"] == "USD"


def test_charge_capability_preserves_required_currency(monkeypatch):
    from unittest.mock import Mock

    from app.services.payment_charge_contracts import (
        ChargeAuthorizationCommand,
        IntentChargeMetadata,
        PaymentGatewayProviderStatus,
        ProviderAcceptance,
    )

    client = _Client(
        {"status": True, "data": {"reference": "currency-ref", "status": "success"}}
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)
    execute = Mock(
        side_effect=lambda action, params: runner.execute(
            _envelope(action, params),
            config={"base_url": "https://gateway.example.test"},
            secret_material={"gateway_credentials": "materialized-test-secret"},
        )
    )
    monkeypatch.setattr(
        payment_capability, "_authorization_executor", Mock(return_value=execute)
    )
    command = ChargeAuthorizationCommand(
        authorization_code="test-authorization",
        email="payer@example.test",
        amount_subunits=10001,
        currency="USD",
        reference="currency-ref",
        metadata=IntentChargeMetadata(uuid4()),
        checkout_binding_id=uuid4(),
    )
    observation = payment_capability.charge_authorization(Mock(), command)
    assert execute.call_args.args[1]["currency"] == "USD"
    assert observation.acceptance is ProviderAcceptance.accepted
    assert observation.provider_status is PaymentGatewayProviderStatus.success
    assert observation.reference == command.reference
    assert observation.amount_subunits is None
    assert observation.currency is None
    assert command.authorization_code not in repr(command)
    assert command.email not in repr(command)


@pytest.mark.parametrize("acceptance", (False, "true", "success", 1, None))
def test_charge_never_infers_acceptance_from_nested_success(monkeypatch, acceptance):
    from unittest.mock import Mock

    from app.services.payment_charge_contracts import (
        ChargeAuthorizationCommand,
        IntentChargeMetadata,
    )

    client = _Client(
        {"status": acceptance, "data": {"status": "success", "reference": "bound-ref"}}
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)

    def execute(action, params):
        return runner.execute(
            _envelope(action, params),
            config={"base_url": "https://gateway.example.test"},
            secret_material={"gateway_credentials": "materialized-test-secret"},
        )

    monkeypatch.setattr(
        payment_capability, "_authorization_executor", Mock(return_value=execute)
    )
    command = ChargeAuthorizationCommand(
        "authorization",
        "payer@example.test",
        10000,
        "NGN",
        "bound-ref",
        IntentChargeMetadata(uuid4()),
        uuid4(),
    )
    with pytest.raises(
        payment_capability.PaymentCapabilityError, match="charge_provider_not_accepted"
    ):
        payment_capability.charge_authorization(Mock(), command)


@pytest.mark.parametrize(
    "status,reference",
    (
        (None, "bound-ref"),
        ("unknown", "bound-ref"),
        ("success", None),
        ("success", "unrelated-ref"),
    ),
)
def test_charge_refuses_missing_unknown_or_unrelated_facts(
    monkeypatch, status, reference
):
    from unittest.mock import Mock

    from app.services.payment_charge_contracts import (
        ChargeAuthorizationCommand,
        IntentChargeMetadata,
    )

    client = _Client(
        {"status": True, "data": {"status": status, "reference": reference}}
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)

    def execute(action, params):
        return runner.execute(
            _envelope(action, params),
            config={"base_url": "https://gateway.example.test"},
            secret_material={"gateway_credentials": "materialized-test-secret"},
        )

    monkeypatch.setattr(
        payment_capability, "_authorization_executor", Mock(return_value=execute)
    )
    command = ChargeAuthorizationCommand(
        "authorization",
        "payer@example.test",
        10000,
        "NGN",
        "bound-ref",
        IntentChargeMetadata(uuid4()),
        uuid4(),
    )
    with pytest.raises(payment_capability.PaymentCapabilityError):
        payment_capability.charge_authorization(Mock(), command)


@pytest.mark.parametrize(
    "field,value",
    (
        ("amount", "10000"),
        ("amount", 100.1),
        ("amount", True),
        ("fees", -1),
        ("currency", ""),
        ("currency", "ngn"),
        ("paid_at", "invalid"),
    ),
)
def test_charge_refuses_present_malformed_money_or_time(field, value):
    client = _Client(
        {
            "status": True,
            "data": {"status": "success", "reference": "bound-ref", field: value},
        }
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)
    result = runner.execute(
        _envelope(
            "charge_authorization",
            {
                "authorization_code": "authorization",
                "email": "payer@example.test",
                "amount_kobo": 10000,
                "currency": "NGN",
                "reference": "bound-ref",
                "metadata": {},
            },
        ),
        config={"base_url": "https://gateway.example.test"},
        secret_material={"gateway_credentials": "materialized-test-secret"},
    )
    assert result.status is OperationStatus.rejected


def test_typed_charge_observation_retains_observed_money_without_expected_defaults(
    monkeypatch,
):
    from unittest.mock import Mock

    from app.services.payment_charge_contracts import (
        ChargeAuthorizationCommand,
        IntentChargeMetadata,
    )

    client = _Client(
        {
            "status": True,
            "data": {
                "status": "success",
                "reference": "bound-ref",
                "amount": 100,
                "currency": "USD",
                "fees": 1,
                "authorization": {"authorization_code": "response-secret"},
                "customer": {"email": "private@example.test"},
            },
        }
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)
    wire_results = []

    def execute(action, params):
        result = runner.execute(
            _envelope(action, params),
            config={"base_url": "https://gateway.example.test"},
            secret_material={"gateway_credentials": "materialized-test-secret"},
        )
        wire_results.append(result)
        return result

    monkeypatch.setattr(
        payment_capability, "_authorization_executor", Mock(return_value=execute)
    )
    observation = payment_capability.charge_authorization(
        Mock(),
        ChargeAuthorizationCommand(
            "authorization",
            "payer@example.test",
            10000,
            "NGN",
            "bound-ref",
            IntentChargeMetadata(uuid4()),
            uuid4(),
        ),
    )
    assert observation.amount_subunits == 100
    assert observation.currency == "USD"
    assert observation.fee_subunits == 1
    assert "response-secret" not in str(wire_results[0].output)
    assert "private@example.test" not in str(wire_results[0].output)


def test_typed_reference_observation_uses_same_verify_transport(monkeypatch):
    from unittest.mock import Mock

    from app.services.payment_charge_contracts import (
        ObserveAuthorizationChargeReference,
        PaymentGatewayProviderStatus,
        ProviderAcceptance,
    )

    client = _Client(
        {"status": True, "data": {"status": "success", "reference": "bound-ref"}}
    )
    runner = PaymentGatewayRunner("paystack", client_override=client)

    def execute(action, params):
        assert action == "verify"
        return runner.execute(
            _envelope(action, params, capability_id=PAYMENT_RECONCILE_CAPABILITY),
            config={"base_url": "https://gateway.example.test"},
            secret_material={"gateway_credentials": "materialized-test-secret"},
        )

    monkeypatch.setattr(
        payment_capability, "_authorization_executor", Mock(return_value=execute)
    )
    observation = payment_capability.observe_authorization_charge_reference(
        Mock(), ObserveAuthorizationChargeReference("bound-ref", uuid4())
    )
    assert observation.acceptance is ProviderAcceptance.accepted
    assert observation.provider_status is PaymentGatewayProviderStatus.success
    assert client.calls[0]["url"].endswith("/transaction/verify/bound-ref")


def test_charge_values_are_frozen_and_binding_identity_is_precise():
    from dataclasses import FrozenInstanceError

    from app.services.payment_charge_contracts import (
        ChargeAuthorizationCommand,
        ChargeProtocolError,
        IntentChargeMetadata,
    )

    command = ChargeAuthorizationCommand(
        "authorization",
        "payer@example.test",
        10000,
        "NGN",
        "bound-ref",
        IntentChargeMetadata(uuid4()),
        uuid4(),
    )
    with pytest.raises(FrozenInstanceError):
        command.reference = "different"
    with pytest.raises(ChargeProtocolError, match="charge_binding_invalid"):
        ChargeAuthorizationCommand(
            "authorization",
            "payer@example.test",
            10000,
            "NGN",
            "bound-ref",
            IntentChargeMetadata(uuid4()),
            "transport-string",
        )


@pytest.mark.parametrize("currency", ["ÑGN", "NＧN", "NG1", "ngn"])
def test_charge_currency_rejects_non_ascii_and_non_iso_shape(currency):
    from app.services.integrations.connectors.payment_gateway import (
        _authorization_payload,
        _observed_currency,
    )
    from app.services.payment_charge_contracts import (
        ChargeAuthorizationCommand,
        ChargeProtocolError,
        IntentChargeMetadata,
    )

    with pytest.raises(ChargeProtocolError, match="charge_currency_invalid"):
        ChargeAuthorizationCommand(
            "authorization",
            "payer@example.test",
            100,
            currency,
            "bound-ref",
            IntentChargeMetadata(uuid4()),
            uuid4(),
        )
    with pytest.raises(ChargeProtocolError, match="charge_currency_invalid"):
        _observed_currency(currency)
    with pytest.raises(ChargeProtocolError, match="charge_currency_invalid"):
        _authorization_payload(
            {
                "authorization_code": "authorization",
                "email": "payer@example.test",
                "reference": "bound-ref",
                "currency": currency,
                "amount_kobo": 100,
                "metadata": {},
            }
        )


@pytest.mark.parametrize("status_code", [400, 404, 500])
def test_typed_reference_transport_preserves_recovery_error_classification(
    monkeypatch, status_code
):
    from unittest.mock import Mock

    from app.services.integrations.runtime import OperationResult
    from app.services.payment_charge_contracts import (
        ObserveAuthorizationChargeReference,
    )

    failed = OperationResult(
        operation_id=uuid4(),
        status=OperationStatus.rejected,
        error_code=f"provider_http_{status_code}",
    )
    monkeypatch.setattr(
        payment_capability,
        "_authorization_executor",
        Mock(return_value=Mock(return_value=failed)),
    )
    with pytest.raises(payment_capability.PaymentCapabilityError) as caught:
        payment_capability.observe_authorization_charge_reference(
            Mock(), ObserveAuthorizationChargeReference("bound-ref", uuid4())
        )
    assert caught.value.status_code == status_code
    assert payment_capability.is_verification_not_found(caught.value) is (
        status_code in {400, 404}
    )
