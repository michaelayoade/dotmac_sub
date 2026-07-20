from __future__ import annotations

import hashlib
import hmac
import json

from app.models.billing import (
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderType,
)
from app.models.integration_platform import IntegrationInbox
from app.services.api_billing_webhooks import process_paystack_webhook
from tests.integration_platform_helpers import enable_payment_provider


def _install(db, monkeypatch, *, with_provider: bool = True):
    monkeypatch.setenv("PAYSTACK_TEST_SECRET", "paystack-test-secret")
    monkeypatch.setenv("PAYSTACK_TEST_PUBLIC", "paystack-public")
    bindings = enable_payment_provider(db, "paystack")
    if with_provider:
        db.add(
            PaymentProvider(
                name="Paystack Test",
                provider_type=PaymentProviderType.paystack,
                is_active=True,
            )
        )
        db.commit()
    return bindings["payments.webhook.v1"]


def _request(
    db,
    payload: dict,
    *,
    secret: str = "paystack-test-secret",  # noqa: S107 - synthetic test material
):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return process_paystack_webhook(db=db, body=body, signature=signature)


def test_verified_payment_event_is_processed_once(db_session, monkeypatch):
    binding = _install(db_session, monkeypatch)
    payload = {
        "event": "transfer.success",
        "data": {"id": 1001, "reference": "payment-inbox-1"},
    }

    first = _request(db_session, payload)
    second = _request(db_session, payload)

    assert first.status_code == 200
    assert second.status_code == 200
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.capability_binding_id == binding.id
    assert receipt.state == "processed"
    assert receipt.attempt_count == 1
    assert db_session.query(PaymentProviderEvent).count() == 1


def test_provider_identity_collision_quarantines_installation(db_session, monkeypatch):
    binding = _install(db_session, monkeypatch)
    first = {
        "event": "transfer.success",
        "data": {"id": 1002, "reference": "payment-collision"},
    }
    changed = {
        "event": "transfer.failed",
        "data": {"id": 1002, "reference": "payment-collision"},
    }

    assert _request(db_session, first).status_code == 200
    assert _request(db_session, changed).status_code == 409
    db_session.refresh(binding.installation)
    assert binding.installation.state == "quarantined"


def test_processing_failure_is_retained_for_replay(db_session, monkeypatch):
    _install(db_session, monkeypatch)
    monkeypatch.setattr(
        "app.services.api_billing_webhooks.billing_service.payment_provider_events.ingest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary")),
    )

    response = _request(
        db_session,
        {
            "event": "transfer.success",
            "data": {"id": 1003, "reference": "payment-retryable"},
        },
    )

    assert response.status_code == 500
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.state == "retryable"
    assert receipt.error_code == "payment_event_processing_failed"


def test_verified_event_without_payment_provider_is_retained(db_session, monkeypatch):
    _install(db_session, monkeypatch, with_provider=False)

    response = _request(
        db_session,
        {
            "event": "transfer.success",
            "data": {"id": 1004, "reference": "payment-no-provider"},
        },
    )

    assert response.status_code == 503
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.state == "retryable"
    assert receipt.error_code == "payment_provider_not_configured"


def test_invalid_signature_is_not_recorded(db_session, monkeypatch):
    _install(db_session, monkeypatch)
    body = b'{"event":"transfer.success","data":{"reference":"bad"}}'

    response = process_paystack_webhook(
        db=db_session,
        body=body,
        signature="invalid",
    )

    assert response.status_code == 400
    assert db_session.query(IntegrationInbox).count() == 0
