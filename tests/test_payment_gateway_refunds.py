from decimal import Decimal

from app.services import flutterwave, paystack
from app.services.payment_gateway_adapter import (
    PaymentGatewayRefundState,
    payment_gateway_adapter,
)


def test_paystack_refund_submission_carries_durable_request_key(
    db_session,
    monkeypatch,
):
    captured = {}

    def refund(_db, reference, amount, *, request_key):
        captured.update(
            reference=reference,
            amount=amount,
            request_key=request_key,
        )
        return {"id": 71, "status": "pending", "amount": 12500}

    monkeypatch.setattr(paystack, "refund_transaction", refund)

    result = payment_gateway_adapter.refund(
        db_session,
        provider_type="paystack",
        reference="funding-ref",
        transaction_id="4455",
        amount=Decimal("125.00"),
        request_key="durable-request-id",
    )

    assert captured == {
        "reference": "funding-ref",
        "amount": Decimal("125.00"),
        "request_key": "durable-request-id",
    }
    assert result.external_id == "71"
    assert result.amount == Decimal("125")
    assert result.state == PaymentGatewayRefundState.pending


def test_paystack_ambiguous_refund_is_found_by_durable_request_key(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        paystack,
        "list_refunds",
        lambda _db, *, transaction_id: [
            {
                "id": 10,
                "status": "processed",
                "amount": 9900,
                "merchant_note": "different-request",
            },
            {
                "id": 11,
                "status": "processed",
                "amount": 12500,
                "merchant_note": "durable-request-id",
            },
        ],
    )

    result = payment_gateway_adapter.find_refund(
        db_session,
        provider_type="paystack",
        transaction_id="4455",
        request_key="durable-request-id",
    )

    assert result is not None
    assert result.external_id == "11"
    assert result.amount == Decimal("125")
    assert result.state == PaymentGatewayRefundState.succeeded


def test_flutterwave_completed_refund_remains_pending_until_final_status(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        flutterwave,
        "fetch_refund",
        lambda _db, refund_id: {
            "id": refund_id,
            "status": "completed",
            "amount_refunded": 125,
        },
    )

    result = payment_gateway_adapter.find_refund(
        db_session,
        provider_type="flutterwave",
        transaction_id="8899",
        request_key="durable-request-id",
        refund_id="refund-44",
    )

    assert result is not None
    assert result.state == PaymentGatewayRefundState.pending
    assert result.status == "completed"


def test_flutterwave_embedded_failed_disbursement_is_terminal(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        flutterwave,
        "fetch_refund",
        lambda _db, refund_id: {
            "id": refund_id,
            "status": "completed",
            "amount_refunded": 125,
            "meta": '{"disburse_status": "failed"}',
        },
    )

    result = payment_gateway_adapter.find_refund(
        db_session,
        provider_type="flutterwave",
        transaction_id="8899",
        request_key="durable-request-id",
        refund_id="refund-44",
    )

    assert result is not None
    assert result.state == PaymentGatewayRefundState.failed
