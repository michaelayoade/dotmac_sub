import importlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.billing import InvoiceStatus, Payment, TopupIntent
from app.models.subscriber import Subscriber
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.customer_portal_flow_billing import get_billing_page
from app.services.customer_portal_flow_payments import (
    create_topup_intent,
    get_topup_page,
    verify_and_record_payment,
    verify_and_record_topup,
)


def _make_invoice(
    db_session, account_id, *, amount: str, invoice_number: str
) -> object:
    return billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=account_id,
            invoice_number=invoice_number,
            currency="NGN",
            subtotal=Decimal(amount),
            total=Decimal(amount),
            balance_due=Decimal(amount),
            status=InvoiceStatus.issued,
        ),
    )


def _patch_topup_settings(
    monkeypatch, *, min_amount: int = 1000, max_amount: int = 500000
) -> None:
    def _fake_resolve_value(_db, _domain, key):
        if key == "topup_min_amount":
            return min_amount
        if key == "topup_max_amount":
            return max_amount
        return None

    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.resolve_value",
        _fake_resolve_value,
    )


def _create_intent(
    monkeypatch, db_session, subscriber, *, amount: str, reference: str
) -> dict:
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference=reference,
        ),
    )
    return create_topup_intent(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
        amount,
        provider="paystack",
    )


def test_get_topup_page_includes_limits_and_public_key(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="unused-ref",
        ),
    )
    _patch_topup_settings(monkeypatch, min_amount=2500, max_amount=750000)

    page = get_topup_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert "payment_reference" not in page
    assert page["provider_public_key"] == "pk_test_topup"
    assert page["min_amount"] == 2500
    assert page["max_amount"] == 750000


def test_get_topup_page_omits_balance_when_lookup_fails(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="unused-ref",
        ),
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.get_available_balance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("lookup failed")),
    )
    _patch_topup_settings(monkeypatch)

    page = get_topup_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert page["prepaid_balance"] is None


def test_get_billing_page_includes_current_balance(monkeypatch, db_session, subscriber):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_billing.get_available_balance",
        lambda *_args, **_kwargs: Decimal("2500.00"),
    )

    page = get_billing_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert page["prepaid_balance"] == Decimal("2500.00")


def test_create_topup_intent_persists_server_owned_reference(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    payload = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="topup-intent-ref-1",
    )

    intent = (
        db_session.query(TopupIntent).filter_by(reference="topup-intent-ref-1").one()
    )

    assert payload["reference"] == "topup-intent-ref-1"
    assert payload["checkout_metadata"]["topup_intent_id"] == str(intent.id)
    assert payload["checkout_metadata"]["account_id"] == str(subscriber.id)
    assert intent.requested_amount == Decimal("5000.00")
    assert intent.status == "pending"


def test_verify_and_record_topup_returns_allocation_breakdown_and_credit_added(
    monkeypatch, db_session, subscriber
):
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="3000.00",
        invoice_number="INV-TOPUP-1",
    )
    _patch_topup_settings(monkeypatch)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-1",
    )

    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-topup-1",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-1",
        provider="paystack",
    )

    db_session.refresh(invoice)

    assert result["already_recorded"] is False
    assert result["allocated_total"] == Decimal("3000.00")
    assert result["credit_added"] == Decimal("2000.00")
    assert result["available_balance"] == Decimal("2000.00")
    assert result["policy_warnings"] == []
    assert result["allocated_to_invoices"] == [
        {
            "invoice_id": str(invoice.id),
            "invoice_number": "INV-TOPUP-1",
            "amount": Decimal("3000.00"),
        }
    ]
    assert invoice.balance_due == Decimal("0.00")


def test_verify_and_record_payment_allocates_invoice_and_credits_remainder(
    monkeypatch, db_session, subscriber
):
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="3000.00",
        invoice_number="INV-PAY-1",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-pay-1",
            memo_prefix="Paystack",
            metadata={"invoice_id": str(invoice.id)},
        ),
    )
    payments_service = importlib.import_module("app.services.billing.payments")
    monkeypatch.setattr(payments_service, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.collections.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.get_available_balance",
        lambda *_args, **_kwargs: Decimal("2000.00"),
    )

    result = verify_and_record_payment(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-pay-1",
        provider="paystack",
    )

    db_session.refresh(invoice)
    payment = db_session.query(Payment).filter_by(external_id="ext-pay-1").one()

    assert result["already_recorded"] is False
    assert result["allocated_total"] == Decimal("3000.00")
    assert result["credit_added"] == Decimal("2000.00")
    assert result["available_balance"] == Decimal("2000.00")
    assert result["allocated_to_invoices"] == [
        {
            "invoice_id": str(invoice.id),
            "invoice_number": "INV-PAY-1",
            "amount": Decimal("3000.00"),
        }
    ]
    assert payment.amount == Decimal("5000.00")
    assert invoice.balance_due == Decimal("0.00")


def test_verify_and_record_topup_rejects_reference_for_other_customer(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-owned",
    )
    other_customer = Subscriber(
        first_name="Other",
        last_name="User",
        email="other@example.com",
    )
    db_session.add(other_customer)
    db_session.commit()
    db_session.refresh(other_customer)

    with pytest.raises(ValueError, match="does not belong to this account"):
        verify_and_record_topup(
            db_session,
            {"account_id": str(other_customer.id)},
            "ref-topup-owned",
            provider="paystack",
        )


def test_verify_and_record_topup_records_out_of_policy_charge_with_warning(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch, min_amount=1000, max_amount=500000)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-low",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("500.00"),
            currency="NGN",
            external_id="ext-topup-low",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-low",
        provider="paystack",
    )

    payments = db_session.query(Payment).filter_by(external_id="ext-topup-low").all()

    assert result["already_recorded"] is False
    assert result["amount"] == Decimal("500.00")
    assert result["allocated_total"] == Decimal("0.00")
    assert result["credit_added"] == Decimal("500.00")
    assert len(payments) == 1
    assert result["policy_warnings"]
    assert (
        "Requested ₦5,000.00 but the provider confirmed ₦500.00."
        in result["policy_warnings"]
    )


def test_verify_and_record_topup_records_amount_above_max_with_warning(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch, min_amount=1000, max_amount=500000)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-high",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("500001.00"),
            currency="NGN",
            external_id="ext-topup-high",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-high",
        provider="paystack",
    )

    payments = db_session.query(Payment).filter_by(external_id="ext-topup-high").all()

    assert result["already_recorded"] is False
    assert result["amount"] == Decimal("500001.00")
    assert result["allocated_total"] == Decimal("0.00")
    assert result["credit_added"] == Decimal("500001.00")
    assert len(payments) == 1
    assert result["policy_warnings"]
    assert (
        "Requested ₦5,000.00 but the provider confirmed ₦500,001.00."
        in result["policy_warnings"]
    )


def test_verify_and_record_topup_is_idempotent_and_preserves_summary(
    monkeypatch, db_session, subscriber
):
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="3000.00",
        invoice_number="INV-TOPUP-2",
    )
    _patch_topup_settings(monkeypatch)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-2",
    )

    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-topup-2",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    first = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-2",
        provider="paystack",
    )
    second = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-2",
        provider="paystack",
    )

    payments = db_session.query(Payment).all()

    assert first["already_recorded"] is False
    assert second["already_recorded"] is True
    assert second["allocated_total"] == Decimal("3000.00")
    assert second["credit_added"] == Decimal("2000.00")
    assert len(payments) == 1
    assert second["allocated_to_invoices"][0]["invoice_id"] == str(invoice.id)


def test_verify_and_record_topup_omits_available_balance_when_lookup_fails(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-balance-miss",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-topup-balance-miss",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.get_available_balance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("balance lookup failed")
        ),
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-balance-miss",
        provider="paystack",
    )

    assert result["already_recorded"] is False
    assert result["available_balance"] is None
