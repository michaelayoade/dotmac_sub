import json
from unittest.mock import MagicMock, patch

from app.services.api_billing_webhooks import (
    process_flutterwave_webhook,
    process_paystack_webhook,
)


def test_paystack_webhook_returns_500_on_ingest_failure(db_session):
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    with patch(
        "app.services.api_billing_webhooks.verify_paystack_signature",
        return_value=True,
    ), patch(
        "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
        return_value=MagicMock(id="provider-1"),
    ), patch(
        "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
        side_effect=RuntimeError("db write failed"),
    ):
        response = process_paystack_webhook(
            db=db_session,
            body=body,
            signature="sig",
        )

    assert response.status_code == 500
    assert response.body == b'{"status":"processing error"}'


def test_flutterwave_webhook_returns_200_on_ingest_failure(db_session):
    """Flutterwave webhook catches ingest errors and still returns 200 to avoid retries."""
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    with patch(
        "app.services.api_billing_webhooks.verify_flutterwave_signature",
        return_value=True,
    ), patch(
        "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
        return_value=MagicMock(id="provider-1"),
    ), patch(
        "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
        side_effect=RuntimeError("db write failed"),
    ):
        response = process_flutterwave_webhook(
            db=db_session,
            body=body,
            signature="sig",
        )

    assert response.status_code == 200
    assert response.body == b'{"status":"ok"}'
