"""Inbound payment-provider webhooks must never silently drop a money event.

Providers treat HTTP 2xx as "delivered, stop retrying". So a processing failure
has to (a) be captured durably for replay and (b) return a non-2xx so the
provider retries. These tests pin that contract for Paystack and Flutterwave.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.billing import (
    PaymentWebhookDeadLetter,
    PaymentWebhookDeadLetterStatus,
)
from app.services.api_billing_webhooks import (
    list_payment_webhook_dead_letters,
    process_flutterwave_webhook,
    process_paystack_webhook,
    replay_payment_webhook_dead_letter,
)


def _dead_letters(db, provider_type=None):
    q = db.query(PaymentWebhookDeadLetter)
    if provider_type:
        q = q.filter(PaymentWebhookDeadLetter.provider_type == provider_type)
    return q.all()


def test_paystack_webhook_returns_500_and_dead_letters_on_ingest_failure(db_session):
    """Transient ingest failure -> HTTP 500 (provider retries) + parked event."""
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    with (
        patch(
            "app.services.api_billing_webhooks.verify_paystack_signature",
            return_value=True,
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000001"),
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
            side_effect=RuntimeError("db write failed"),
        ),
    ):
        response = process_paystack_webhook(db=db_session, body=body, signature="sig")

    assert response.status_code == 500
    assert response.body == b'{"status":"error"}'

    rows = _dead_letters(db_session, "paystack")
    assert len(rows) == 1
    assert rows[0].status == PaymentWebhookDeadLetterStatus.failed
    assert rows[0].idempotency_key == "paystack-1"
    assert "db write failed" in (rows[0].error or "")


def test_flutterwave_webhook_returns_500_and_dead_letters_on_ingest_failure(db_session):
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    with (
        patch(
            "app.services.api_billing_webhooks.verify_flutterwave_signature",
            return_value=True,
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000001"),
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
            side_effect=RuntimeError("db write failed"),
        ),
    ):
        response = process_flutterwave_webhook(
            db=db_session, body=body, signature="sig"
        )

    assert response.status_code == 500
    rows = _dead_letters(db_session, "flutterwave")
    assert len(rows) == 1
    assert rows[0].status == PaymentWebhookDeadLetterStatus.failed


def test_webhook_success_deletes_dead_letter(db_session):
    """A clean ingest leaves no insurance row behind."""
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    with (
        patch(
            "app.services.api_billing_webhooks.verify_paystack_signature",
            return_value=True,
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000001"),
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
            return_value=MagicMock(),
        ),
    ):
        response = process_paystack_webhook(db=db_session, body=body, signature="sig")

    assert response.status_code == 200
    assert response.body == b'{"status":"ok"}'
    assert _dead_letters(db_session, "paystack") == []


def test_webhook_rejects_bad_data_with_4xx_and_parks_as_rejected(db_session):
    """A deterministic 4xx from ingest is surfaced (not retried as 5xx) and the
    event is parked as ``rejected`` for human review rather than auto-replay."""
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    with (
        patch(
            "app.services.api_billing_webhooks.verify_paystack_signature",
            return_value=True,
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000001"),
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
            side_effect=HTTPException(status_code=400, detail="bad data"),
        ),
    ):
        response = process_paystack_webhook(db=db_session, body=body, signature="sig")

    assert response.status_code == 400
    rows = _dead_letters(db_session, "paystack")
    assert len(rows) == 1
    assert rows[0].status == PaymentWebhookDeadLetterStatus.rejected


def test_provider_retry_reuses_dead_letter_row(db_session):
    """Re-delivery of the same unresolved event bumps retry_count, not row count."""
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    with (
        patch(
            "app.services.api_billing_webhooks.verify_paystack_signature",
            return_value=True,
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000001"),
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
            side_effect=RuntimeError("db write failed"),
        ),
    ):
        process_paystack_webhook(db=db_session, body=body, signature="sig")
        process_paystack_webhook(db=db_session, body=body, signature="sig")

    rows = _dead_letters(db_session, "paystack")
    assert len(rows) == 1
    assert rows[0].retry_count == 1  # second delivery incremented from 0


def test_replay_reprocesses_and_marks_replayed(db_session):
    body = json.dumps({"event": "charge.success", "data": {"id": "1"}}).encode()

    # First, a failure parks a dead-letter row.
    with (
        patch(
            "app.services.api_billing_webhooks.verify_paystack_signature",
            return_value=True,
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000001"),
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
            side_effect=RuntimeError("db write failed"),
        ),
    ):
        process_paystack_webhook(db=db_session, body=body, signature="sig")

    row = _dead_letters(db_session, "paystack")[0]
    assert row.status == PaymentWebhookDeadLetterStatus.failed

    # Now replay succeeds.
    with (
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_providers.get_by_type",
            return_value=MagicMock(id="00000000-0000-0000-0000-000000000001"),
        ),
        patch(
            "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
            return_value=MagicMock(),
        ),
    ):
        result = replay_payment_webhook_dead_letter(db_session, str(row.id))

    assert result.status == PaymentWebhookDeadLetterStatus.replayed


def test_replay_missing_row_404(db_session):
    with pytest.raises(HTTPException) as exc:
        replay_payment_webhook_dead_letter(
            db_session, "00000000-0000-0000-0000-0000000000ff"
        )
    assert exc.value.status_code == 404


def _seed_dead_letter(db, *, provider_type, key, status):
    row = PaymentWebhookDeadLetter(
        provider_type=provider_type,
        idempotency_key=key,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def test_list_dead_letters_filters_by_provider_and_status(db_session):
    _seed_dead_letter(
        db_session,
        provider_type="paystack",
        key="paystack-a",
        status=PaymentWebhookDeadLetterStatus.failed,
    )
    _seed_dead_letter(
        db_session,
        provider_type="flutterwave",
        key="flutterwave-b",
        status=PaymentWebhookDeadLetterStatus.rejected,
    )

    all_rows = list_payment_webhook_dead_letters(db_session)
    assert all_rows["count"] >= 2

    paystack_only = list_payment_webhook_dead_letters(
        db_session, provider_type="paystack"
    )
    assert paystack_only["items"]
    assert all(i.provider_type == "paystack" for i in paystack_only["items"])

    failed_only = list_payment_webhook_dead_letters(db_session, status="failed")
    assert all(
        i.status == PaymentWebhookDeadLetterStatus.failed
        for i in failed_only["items"]
    )


def test_list_dead_letters_rejects_unknown_status(db_session):
    with pytest.raises(HTTPException) as exc:
        list_payment_webhook_dead_letters(db_session, status="nonsense")
    assert exc.value.status_code == 400
