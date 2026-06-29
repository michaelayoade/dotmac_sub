"""Unit tests for the customer-initiated online payment API endpoints.

These cover the thin endpoint layer added in app/api/billing.py
(`initiate_payment` / `verify_payment`): principal scoping, mapping of the
portal payment-service output onto the response schemas, and error translation.
The underlying portal payment services are exercised separately by
tests/test_customer_portal_billing_routes.py and ..._topup_flow.py.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import billing as billing_api
from app.schemas.billing import PaymentInitiateRequest, PaymentVerifyRequest


def _subscriber_principal():
    sub_id = str(uuid.uuid4())
    return {
        "subscriber_id": sub_id,
        "person_id": sub_id,
        "principal_id": sub_id,
        "principal_type": "subscriber",
        "session_id": str(uuid.uuid4()),
        "roles": [],
        "scopes": [],
    }


def _system_user_principal():
    p = _subscriber_principal()
    p["principal_type"] = "system_user"
    return p


def test_customer_from_principal_uses_subscriber_id_as_account():
    p = _subscriber_principal()
    customer = billing_api._customer_from_principal(p)
    assert customer["account_id"] == p["subscriber_id"]
    assert customer["subscriber_id"] == p["subscriber_id"]


def test_initiate_payment_maps_context(monkeypatch):
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_number="INV-001",
        balance_due=Decimal("2500.00"),
        total=Decimal("3000.00"),
        currency="NGN",
    )
    monkeypatch.setattr(
        billing_api.customer_payments,
        "create_invoice_payment_intent",
        lambda db, customer, invoice_id, **kw: {
            "invoice_number": "INV-001",
            "amount": Decimal("2500.00"),
            "currency": "NGN",
            "provider_type": "paystack",
            "provider_public_key": "pk_test_123",
            "reference": "ref_abc",
            "customer_email": "c@example.com",
            "charged": False,
            "checkout_url": None,
        },
    )

    resp = billing_api.initiate_payment(
        PaymentInitiateRequest(invoice_id=invoice.id),
        db=None,
        principal=_subscriber_principal(),
    )

    assert resp.invoice_id == invoice.id
    assert resp.invoice_number == "INV-001"
    # Should bill the outstanding balance, not the gross total.
    assert resp.amount == Decimal("2500.00")
    assert resp.provider_type == "paystack"
    assert resp.provider_public_key == "pk_test_123"
    assert resp.payment_reference == "ref_abc"
    assert resp.customer_email == "c@example.com"
    assert resp.charged is False


def test_initiate_payment_400_when_not_payable(monkeypatch):
    def _raise(db, customer, invoice_id, **kw):
        raise ValueError("Invoice is no longer payable")

    monkeypatch.setattr(
        billing_api.customer_payments,
        "create_invoice_payment_intent",
        _raise,
    )
    with pytest.raises(HTTPException) as exc:
        billing_api.initiate_payment(
            PaymentInitiateRequest(invoice_id=uuid.uuid4()),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 400


def test_initiate_payment_400_with_friendly_saved_card_charge_error(monkeypatch):
    def _boom(db, customer, invoice_id, **kw):
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(
        billing_api.customer_payments,
        "create_invoice_payment_intent",
        _boom,
    )

    with pytest.raises(HTTPException) as exc:
        billing_api.initiate_payment(
            PaymentInitiateRequest(
                invoice_id=uuid.uuid4(),
                payment_method_id=uuid.uuid4(),
                idempotency_key="idem-1",
            ),
            db=None,
            principal=_subscriber_principal(),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == (
        "We could not charge that saved card. Please use another payment method "
        "or try again later."
    )


def test_initiate_payment_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        billing_api.initiate_payment(
            PaymentInitiateRequest(invoice_id=uuid.uuid4()),
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_verify_payment_maps_result(monkeypatch):
    invoice = SimpleNamespace(id=uuid.uuid4())
    payment = SimpleNamespace(
        id=uuid.uuid4(),
        amount=Decimal("2500.00"),
        currency="NGN",
        status=SimpleNamespace(value="succeeded"),
    )
    monkeypatch.setattr(
        billing_api.customer_payments,
        "verify_and_record_payment",
        lambda db, customer, reference, provider=None: {
            "payment": payment,
            "invoice": invoice,
            "amount": Decimal("2500.00"),
            "already_recorded": False,
        },
    )

    resp = billing_api.verify_payment(
        PaymentVerifyRequest(reference="ref_abc"),
        db=None,
        principal=_subscriber_principal(),
    )

    assert resp.payment_id == payment.id
    assert resp.invoice_id == invoice.id
    assert resp.amount == Decimal("2500.00")
    assert resp.status == "succeeded"
    assert resp.already_recorded is False


def test_verify_payment_surfaces_card_save_failure_without_failing(monkeypatch):
    invoice = SimpleNamespace(id=uuid.uuid4())
    payment = SimpleNamespace(
        id=uuid.uuid4(),
        amount=Decimal("2500.00"),
        currency="NGN",
        status=SimpleNamespace(value="succeeded"),
    )
    monkeypatch.setattr(
        billing_api.customer_payments,
        "verify_and_record_payment",
        lambda db, customer, reference, provider=None: {
            "payment": payment,
            "invoice": invoice,
            "amount": Decimal("2500.00"),
            "already_recorded": False,
        },
    )

    def _capture_boom(db, account_id, reference, provider):
        raise RuntimeError("provider token missing")

    monkeypatch.setattr(
        "app.services.customer_portal_flow_payment_methods.capture_card_after_payment",
        _capture_boom,
    )

    resp = billing_api.verify_payment(
        PaymentVerifyRequest(reference="ref_abc", provider="paystack", save_card=True),
        db=None,
        principal=_subscriber_principal(),
    )

    assert resp.payment_id == payment.id
    assert resp.card_saved is False
    assert resp.card_save_message == (
        "Payment was recorded, but we could not save this card. "
        "You can add a card from Payment Methods."
    )


def test_verify_payment_translates_value_error(monkeypatch):
    def _boom(db, customer, reference, provider=None):
        raise ValueError("Invoice not found or access denied")

    monkeypatch.setattr(
        billing_api.customer_payments, "verify_and_record_payment", _boom
    )
    with pytest.raises(HTTPException) as exc:
        billing_api.verify_payment(
            PaymentVerifyRequest(reference="ref_abc"),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 400
    assert "access denied" in exc.value.detail


def test_verify_payment_403_for_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        billing_api.verify_payment(
            PaymentVerifyRequest(reference="ref_abc"),
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403
