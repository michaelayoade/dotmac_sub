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
