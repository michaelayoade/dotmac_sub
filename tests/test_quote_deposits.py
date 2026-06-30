"""Quote deposit collection via the existing billing surface: initiate + verify."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.billing import InvoiceStatus
from app.models.quote_mirror import QuoteMirror
from app.models.subscriber import Subscriber
from app.services import quote_deposits


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="C", last_name="R", email=f"c-{uuid.uuid4().hex[:8]}@example.com"
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _quote(db, sub, *, deposit="37500.00", paid=False) -> QuoteMirror:
    row = QuoteMirror(
        crm_quote_id=f"q-{uuid.uuid4().hex[:8]}",
        subscriber_id=sub.id,
        status="draft",
        currency="NGN",
        total="75000.00",
        deposit_amount=deposit,
        deposit_paid=paid,
        payload={"id": "q1", "status": "draft", "deposit_amount": deposit},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _customer(sub):
    return {
        "account_id": str(sub.id),
        "subscriber_id": str(sub.id),
        "username": "c@example.com",
    }


def test_initiate_creates_invoice_and_returns_checkout(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub)
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
            db_session, _customer(sub), str(sub.id), row.crm_quote_id
        )
    assert out["amount"] == "37500.00"
    assert out["payment_reference"] == "ref_1"
    assert out["invoice_id"] == str(fake_invoice.id)
    intent_fn.assert_called_once()


def test_initiate_rejects_when_already_paid(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub, paid=True)
    with pytest.raises(HTTPException) as exc:
        quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id
        )
    assert exc.value.status_code == 409


def test_initiate_rejects_when_no_deposit(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub, deposit="0")
    with pytest.raises(HTTPException) as exc:
        quote_deposits.initiate_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id
        )
    assert exc.value.status_code == 400


def test_verify_paid_accepts_quote(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub)
    paid_invoice = SimpleNamespace(status=InvoiceStatus.paid)
    accepted = {"id": row.crm_quote_id, "status": "accepted", "deposit_paid": True}
    with (
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": paid_invoice, "amount": Decimal("37500.00")},
        ),
        patch(
            "app.services.quote_deposits.quotes_mirror.accept_quote",
            return_value=accepted,
        ) as accept_fn,
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id, reference="ref_1"
        )
    assert out["paid"] is True
    assert out["quote"]["status"] == "accepted"
    accept_fn.assert_called_once()


def test_verify_unpaid_does_not_accept(db_session):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub)
    pending_invoice = SimpleNamespace(status=InvoiceStatus.issued)
    with (
        patch(
            "app.services.quote_deposits.payments.verify_and_record_payment",
            return_value={"invoice": pending_invoice, "amount": Decimal("0.00")},
        ),
        patch("app.services.quote_deposits.quotes_mirror.accept_quote") as accept_fn,
    ):
        out = quote_deposits.verify_deposit(
            db_session, _customer(sub), str(sub.id), row.crm_quote_id, reference="ref_1"
        )
    assert out["paid"] is False
    accept_fn.assert_not_called()
