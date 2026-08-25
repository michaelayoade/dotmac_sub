"""Provider adapters expose typed facts without deciding intent lifecycle."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.services.integrations.payment_capability import PaymentCapabilityError
from app.services.payment_gateway_adapter import (
    PaymentGatewayProviderStatus,
    PaymentGatewayVerificationOutcome,
    PaymentGatewayVerificationReason,
    payment_gateway_adapter,
)


@pytest.mark.parametrize(
    ("provider", "status", "outcome", "reason"),
    (
        (
            "paystack",
            "failed",
            PaymentGatewayVerificationOutcome.failed,
            PaymentGatewayVerificationReason.provider_reported_failed,
        ),
        (
            "paystack",
            "abandoned",
            PaymentGatewayVerificationOutcome.abandoned,
            PaymentGatewayVerificationReason.provider_reported_abandoned,
        ),
        (
            "paystack",
            "pending",
            PaymentGatewayVerificationOutcome.awaiting_confirmation,
            PaymentGatewayVerificationReason.provider_awaiting_confirmation,
        ),
        (
            "paystack",
            "ongoing",
            PaymentGatewayVerificationOutcome.processing,
            PaymentGatewayVerificationReason.provider_reported_processing,
        ),
        (
            "paystack",
            "processing",
            PaymentGatewayVerificationOutcome.processing,
            PaymentGatewayVerificationReason.provider_reported_processing,
        ),
        (
            "flutterwave",
            "failed",
            PaymentGatewayVerificationOutcome.failed,
            PaymentGatewayVerificationReason.provider_reported_failed,
        ),
        (
            "flutterwave",
            "pending",
            PaymentGatewayVerificationOutcome.awaiting_confirmation,
            PaymentGatewayVerificationReason.provider_awaiting_confirmation,
        ),
    ),
)
def test_gateway_statuses_are_normalized_without_business_mutation(
    monkeypatch, provider, status, outcome, reason
):
    monkeypatch.setattr(
        "app.services.payment_gateway_adapter.payment_capability.verify_transaction",
        Mock(return_value={"status": status}),
    )

    observation = payment_gateway_adapter.observe_verification(
        Mock(), provider_type=provider, reference="test-reference"
    )

    assert observation.outcome is outcome
    assert observation.reason_code is reason
    assert observation.provider_status is PaymentGatewayProviderStatus(status)
    assert observation.transaction is None


@pytest.mark.parametrize(
    ("error", "outcome", "reason"),
    (
        (
            PaymentCapabilityError("provider_http_404"),
            PaymentGatewayVerificationOutcome.unknown,
            PaymentGatewayVerificationReason.provider_reference_not_found,
        ),
        (
            PaymentCapabilityError("provider_http_503"),
            PaymentGatewayVerificationOutcome.unavailable,
            PaymentGatewayVerificationReason.provider_unavailable,
        ),
    ),
)
def test_ambiguous_or_unavailable_verification_fails_closed(
    monkeypatch, error, outcome, reason
):
    monkeypatch.setattr(
        "app.services.payment_gateway_adapter.payment_capability.verify_transaction",
        Mock(side_effect=error),
    )

    observation = payment_gateway_adapter.observe_verification(
        Mock(), provider_type="paystack", reference="test-reference"
    )

    assert observation.outcome is outcome
    assert observation.reason_code is reason
    assert observation.transaction is None


def test_http_success_uses_transaction_status_not_transport_success(monkeypatch):
    monkeypatch.setattr(
        "app.services.payment_gateway_adapter.payment_capability.verify_transaction",
        Mock(return_value={"status": "unrecognized"}),
    )

    observation = payment_gateway_adapter.observe_verification(
        Mock(), provider_type="paystack", reference="test-reference"
    )

    assert observation.outcome is PaymentGatewayVerificationOutcome.unknown
    assert observation.transaction is None


@pytest.mark.parametrize("status", ("successful", "succeeded"))
def test_flutterwave_success_vocabulary_produces_settlement_evidence(
    monkeypatch, status
):
    monkeypatch.setattr(
        "app.services.payment_gateway_adapter.payment_capability.verify_transaction",
        Mock(
            return_value={
                "id": "transaction-test-id",
                "status": status,
                "amount": "1050.00",
                "app_fee": "50.00",
                "currency": "NGN",
                "meta": {},
            }
        ),
    )

    observation = payment_gateway_adapter.observe_verification(
        Mock(), provider_type="flutterwave", reference="test-reference"
    )

    assert observation.outcome is PaymentGatewayVerificationOutcome.succeeded
    assert observation.provider_status is PaymentGatewayProviderStatus(status)
    assert observation.transaction is not None
    assert observation.transaction.external_id == "transaction-test-id"


def test_success_status_with_incomplete_transaction_evidence_fails_closed(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.payment_gateway_adapter.payment_capability.verify_transaction",
        Mock(return_value={"status": "success", "amount": 500_000}),
    )

    observation = payment_gateway_adapter.observe_verification(
        Mock(), provider_type="paystack", reference="test-reference"
    )

    assert observation.outcome is PaymentGatewayVerificationOutcome.unknown
    assert (
        observation.reason_code
        is PaymentGatewayVerificationReason.provider_evidence_incomplete
    )
    assert observation.transaction is None


@pytest.mark.parametrize(
    ("provider", "status"),
    (
        ("paystack", "successful"),
        ("flutterwave", "abandoned"),
        ("flutterwave", "ongoing"),
    ),
)
def test_status_from_another_provider_vocabulary_fails_closed(
    monkeypatch, provider, status
):
    monkeypatch.setattr(
        "app.services.payment_gateway_adapter.payment_capability.verify_transaction",
        Mock(return_value={"status": status}),
    )

    observation = payment_gateway_adapter.observe_verification(
        Mock(), provider_type=provider, reference="test-reference"
    )

    assert observation.outcome is PaymentGatewayVerificationOutcome.unknown
    assert (
        observation.reason_code
        is PaymentGatewayVerificationReason.provider_status_unknown
    )
    assert observation.transaction is None
