"""Native quote deposit collection through Sub's billing surface."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.billing import InvoiceStatus, Payment
from app.models.sales import SalesOrder
from app.models.subscriber import Subscriber
from app.services import quote_deposits
from app.services.sales import selfserve


_FAP = SimpleNamespace(id=uuid.uuid4(), name="NAP-041")


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="C", last_name="R", email=f"c-{uuid.uuid4().hex[:8]}@example.com"
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _customer(sub):
    return {
        "account_id": str(sub.id),
        "subscriber_id": str(sub.id),
        "username": "c@example.com",
    }


def _native_quote(db, sub):
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(_FAP, 1300.0),
    ):
        return selfserve.selfserve_quotes.request_quote(
            db,
            str(sub.id),
            latitude=9.0765,
            longitude=7.3986,
            address="12 Mississippi St, Maitama",
        )


def test_initiate_creates_invoice_and_returns_checkout(db_session):
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    fake_invoice = SimpleNamespace(id=uuid.uuid4(), metadata_=None)
    intent = {
        "provider_type": "paystack",
        "reference": "ref_1",
        "checkout_url": None,
        "currency": "NGN",
    }
    with (
        patch(
            "app.services.quote_deposits.billing_service.invoices.create",
            return_value=fake_invoice,
        ),
        patch(
            "app.services.quote_deposits.payments.create_invoice_payment_intent",
            return_value=intent,
        ) as intent_fn,
    ):
        out = quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id)
        )
    assert out["amount"] == "37500.00"
    assert out["payment_reference"] == "ref_1"
    assert out["invoice_id"] == str(fake_invoice.id)
    intent_fn.assert_called_once()


def test_initiate_rejects_when_already_paid(db_session):
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    metadata = dict(quote.metadata_ or {})
    metadata["deposit"] = {"amount": "37500.00", "paid": True}
    quote.metadata_ = metadata
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id)
        )
    assert exc.value.status_code == 409


def test_verify_paid_accepts_natively_and_is_idempotent(db_session):
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    paid_invoice = SimpleNamespace(status=InvoiceStatus.paid)
    with patch(
        "app.services.quote_deposits.payments.verify_and_record_payment",
        return_value={"invoice": paid_invoice, "amount": Decimal("37500.00")},
    ):
        first = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id), reference="ref_1"
        )
        second = quote_deposits.verify_deposit(
            db_session,
            _customer(sub),
            str(sub.id),
            str(quote.id),
            reference="ref_1_retry",
        )

    assert first["paid"] is True
    assert first["quote"]["status"] == "accepted"
    assert first["quote"]["deposit_reference"] == "ref_1"
    assert second["quote"]["already_accepted"] is True
    assert second["quote"]["sales_order_id"] == first["quote"]["sales_order_id"]
    assert second["quote"]["deposit_reference"] == "ref_1"

    order = db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).one()
    assert order.deposit_paid is True
    assert order.amount_paid == Decimal("37500.00")
    assert order.payment_status == "partial"
    assert db_session.query(Payment).count() == 0


def test_verify_unpaid_leaves_native_quote_draft(db_session):
    sub = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    pending_invoice = SimpleNamespace(status=InvoiceStatus.issued)
    with patch(
        "app.services.quote_deposits.payments.verify_and_record_payment",
        return_value={"invoice": pending_invoice, "amount": Decimal("0.00")},
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), str(quote.id), reference="ref_1"
        )
    assert out["paid"] is False
    assert out["quote"]["status"] == "draft"
    assert db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).count() == 0


def test_verify_is_subscriber_scoped(db_session):
    sub = _subscriber(db_session)
    other = _subscriber(db_session)
    quote = _native_quote(db_session, sub)
    with pytest.raises(HTTPException) as exc:
        quote_deposits.verify_deposit(
            db_session,
            _customer(other),
            str(other.id),
            str(quote.id),
            reference="ref_1",
        )
    assert exc.value.status_code == 404
