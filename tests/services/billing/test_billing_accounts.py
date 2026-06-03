"""Tests for the BillingAccount manager."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.subscriber import Reseller
from app.schemas.billing import BillingAccountCreate, BillingAccountUpdate
from app.services import billing as billing_service


def _make_reseller(db_session, *, name: str = "Acme Partner", is_house: bool = False):
    r = Reseller(name=name, is_house=is_house)
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def test_create_default_for_reseller_creates_one(db_session):
    reseller = _make_reseller(db_session)
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    assert ba.reseller_id == reseller.id
    assert ba.name == reseller.name
    assert ba.balance == Decimal("0.00")
    assert ba.currency == "NGN"


def test_create_default_for_reseller_is_idempotent(db_session):
    reseller = _make_reseller(db_session)
    first = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    second = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    assert first.id == second.id


def test_get_for_reseller_lazy_creates(db_session):
    reseller = _make_reseller(db_session, name="Lazy")
    ba = billing_service.billing_accounts.get_for_reseller(db_session, str(reseller.id))
    assert ba.reseller_id == reseller.id


def test_create_via_payload(db_session):
    reseller = _make_reseller(db_session, name="ViaPayload")
    ba = billing_service.billing_accounts.create(
        db_session,
        BillingAccountCreate(
            reseller_id=reseller.id, name="Custom name", currency="USD"
        ),
    )
    assert ba.name == "Custom name"
    assert ba.currency == "USD"


def test_create_conflict_when_already_exists(db_session):
    reseller = _make_reseller(db_session, name="Conflict")
    billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    with pytest.raises(HTTPException) as exc:
        billing_service.billing_accounts.create(
            db_session,
            BillingAccountCreate(reseller_id=reseller.id, name="Dup"),
        )
    assert exc.value.status_code == 409


def test_update_renames(db_session):
    reseller = _make_reseller(db_session, name="Renamer")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    updated = billing_service.billing_accounts.update(
        db_session, str(ba.id), BillingAccountUpdate(name="Renamed")
    )
    assert updated.name == "Renamed"


def test_credit_and_debit_balance(db_session):
    reseller = _make_reseller(db_session, name="Balance")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    billing_service.billing_accounts.credit_balance(
        db_session, str(ba.id), Decimal("250.00")
    )
    db_session.refresh(ba)
    assert ba.balance == Decimal("250.00")

    billing_service.billing_accounts.debit_balance(
        db_session, str(ba.id), Decimal("100.00")
    )
    db_session.refresh(ba)
    assert ba.balance == Decimal("150.00")


def test_debit_balance_insufficient(db_session):
    reseller = _make_reseller(db_session, name="Insufficient")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    with pytest.raises(HTTPException) as exc:
        billing_service.billing_accounts.debit_balance(
            db_session, str(ba.id), Decimal("1.00")
        )
    assert exc.value.status_code == 400


def test_list_filters_by_reseller(db_session):
    r1 = _make_reseller(db_session, name="A")
    r2 = _make_reseller(db_session, name="B")
    billing_service.billing_accounts.create_default_for_reseller(db_session, str(r1.id))
    billing_service.billing_accounts.create_default_for_reseller(db_session, str(r2.id))
    only_r1 = billing_service.billing_accounts.list(
        db_session, reseller_id=str(r1.id)
    )
    assert len(only_r1) == 1
    assert only_r1[0].reseller_id == r1.id


def test_statement_aggregates_open_invoices(db_session, subscriber):
    """Statement aggregates open invoices for subscribers in the reseller."""
    from app.models.billing import Invoice, InvoiceStatus

    reseller = _make_reseller(db_session, name="WithInvoices")
    # Wire the existing subscriber fixture under this reseller.
    subscriber.reseller_id = reseller.id
    db_session.add(subscriber)
    db_session.flush()

    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    # Open invoice for subscriber.
    inv = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("500.00"),
        balance_due=Decimal("500.00"),
    )
    db_session.add(inv)
    db_session.commit()

    statement = billing_service.billing_accounts.statement(db_session, str(ba.id))
    assert statement.total_outstanding == Decimal("500.00")
    assert len(statement.subscribers) == 1
    assert statement.subscribers[0].open_invoice_count == 1
    assert statement.subscribers[0].open_balance == Decimal("500.00")
