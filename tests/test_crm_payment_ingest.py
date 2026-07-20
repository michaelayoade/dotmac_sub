from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import crm as crm_routes
from app.models.billing import Invoice, InvoiceStatus, Payment
from app.models.subscriber import Subscriber
from app.services import crm_api


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Ada",
        last_name="L",
        email=f"a-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"SUB-{uuid.uuid4().hex[:6]}",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def test_record_payment_creates_ledger_payment(db_session):
    sub = _subscriber(db_session)
    payment = crm_api.record_external_payment(
        db_session, subscriber_id=str(sub.id), amount="5000", external_ref="so-1"
    )
    assert payment.external_id == "crm:so-1"
    assert str(payment.account_id) == str(sub.id)
    assert (
        db_session.query(Payment).filter(Payment.external_id == "crm:so-1").count() == 1
    )


def test_record_payment_is_idempotent(db_session):
    sub = _subscriber(db_session)
    p1 = crm_api.record_external_payment(
        db_session, subscriber_id=str(sub.id), amount="5000", external_ref="so-2"
    )
    p2 = crm_api.record_external_payment(
        db_session, subscriber_id=str(sub.id), amount="5000", external_ref="so-2"
    )
    assert p1.id == p2.id
    assert (
        db_session.query(Payment).filter(Payment.external_id == "crm:so-2").count() == 1
    )


def test_record_payment_allocates_to_matching_invoice(db_session):
    sub = _subscriber(db_session)
    inv = Invoice(
        account_id=sub.id,
        status=InvoiceStatus.issued,
        subtotal=Decimal("5000"),
        total=Decimal("5000"),
        balance_due=Decimal("5000"),
        metadata_={"crm_external_ref": "install-1", "source": "dotmac_crm"},
    )
    db_session.add(inv)
    db_session.commit()

    crm_api.record_external_payment(
        db_session,
        subscriber_id=str(sub.id),
        amount="5000",
        external_ref="pay-install-1",
        invoice_external_ref="install-1",
    )
    db_session.refresh(inv)
    assert inv.balance_due == Decimal("0.00")
    assert inv.status == InvoiceStatus.paid


def test_endpoint_requires_fields(db_session):
    with pytest.raises(HTTPException) as exc:
        crm_routes.record_crm_payment(payload={}, db=db_session)
    assert exc.value.status_code == 400


def test_endpoint_404_unknown_subscriber(db_session):
    with pytest.raises(HTTPException) as exc:
        crm_routes.record_crm_payment(
            payload={
                "subscriber_id": str(uuid.uuid4()),
                "amount": "100",
                "external_ref": "x",
            },
            db=db_session,
        )
    assert exc.value.status_code == 404
