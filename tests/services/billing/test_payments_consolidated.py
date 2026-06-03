"""Tests for consolidated (billing-account-scoped) payment allocation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    PaymentAllocation,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service


def _make_reseller(db_session, *, name: str = "Partner"):
    r = Reseller(name=name)
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def _make_subscriber(db_session, *, reseller_id, suffix: str):
    sub = Subscriber(
        first_name="Sub",
        last_name=suffix,
        email=f"sub-{suffix.lower()}@example.com",
        reseller_id=reseller_id,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _make_invoice(db_session, *, account_id, balance: Decimal):
    inv = Invoice(
        account_id=account_id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=balance,
        balance_due=balance,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    return inv


def test_paymentcreate_requires_exactly_one_account_scope():
    with pytest.raises(ValueError):
        PaymentCreate(amount=Decimal("10.00"))


def test_consolidated_payment_auto_allocates_across_subscribers(db_session):
    """One consolidated payment FIFO-allocates across multiple subscribers."""
    reseller = _make_reseller(db_session)
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub_a = _make_subscriber(db_session, reseller_id=reseller.id, suffix="A")
    sub_b = _make_subscriber(db_session, reseller_id=reseller.id, suffix="B")
    inv_a = _make_invoice(db_session, account_id=sub_a.id, balance=Decimal("300.00"))
    inv_b = _make_invoice(db_session, account_id=sub_b.id, balance=Decimal("200.00"))

    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("450.00"),
            currency="NGN",
        ),
    )
    assert payment.billing_account_id == ba.id
    assert payment.account_id is None

    db_session.refresh(inv_a)
    db_session.refresh(inv_b)
    db_session.refresh(ba)
    allocated_total = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    allocations_by_invoice = {
        str(a.invoice_id): a.amount for a in allocated_total
    }
    # Both invoices got allocations.
    assert str(inv_a.id) in allocations_by_invoice
    assert str(inv_b.id) in allocations_by_invoice
    assert (
        allocations_by_invoice[str(inv_a.id)]
        + allocations_by_invoice[str(inv_b.id)]
        == Decimal("450.00")
    )
    # Unallocated balance is zero (everything fit).
    assert ba.balance == Decimal("0.00")


def test_consolidated_payment_remainder_credits_billing_account_balance(db_session):
    reseller = _make_reseller(db_session, name="WithRemainder")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="C")
    _make_invoice(db_session, account_id=sub.id, balance=Decimal("100.00"))

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("250.00"),
            currency="NGN",
        ),
    )
    db_session.refresh(ba)
    # 100 allocated, 150 surplus on the billing account.
    assert ba.balance == Decimal("150.00")


def test_consolidated_payment_ledger_entries_are_per_subscriber(db_session):
    reseller = _make_reseller(db_session, name="LedgerCheck")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub_a = _make_subscriber(db_session, reseller_id=reseller.id, suffix="LA")
    sub_b = _make_subscriber(db_session, reseller_id=reseller.id, suffix="LB")
    _make_invoice(db_session, account_id=sub_a.id, balance=Decimal("100.00"))
    _make_invoice(db_session, account_id=sub_b.id, balance=Decimal("100.00"))

    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("200.00"),
            currency="NGN",
        ),
    )
    entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .all()
    )
    # One ledger entry per subscriber.
    account_ids = {str(e.account_id) for e in entries}
    assert {str(sub_a.id), str(sub_b.id)} <= account_ids
    # And every entry has a per-subscriber account_id (none with NULL).
    assert all(e.account_id is not None for e in entries)


def test_consolidated_explicit_allocation_rejects_cross_reseller_invoice(db_session):
    r1 = _make_reseller(db_session, name="R1")
    r2 = _make_reseller(db_session, name="R2")
    ba1 = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(r1.id)
    )
    sub_other = _make_subscriber(db_session, reseller_id=r2.id, suffix="OTHER")
    inv_other = _make_invoice(
        db_session, account_id=sub_other.id, balance=Decimal("100.00")
    )

    from app.schemas.billing import PaymentAllocationApply

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.create(
            db_session,
            PaymentCreate(
                billing_account_id=ba1.id,
                amount=Decimal("100.00"),
                currency="NGN",
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=inv_other.id, amount=Decimal("100.00")
                    )
                ],
            ),
        )
    assert exc.value.status_code == 400


def test_account_scoped_payment_still_rejects_cross_account_invoice(db_session, subscriber):
    """Regression: existing single-account flow still enforces same-account."""
    other_reseller = _make_reseller(db_session, name="Other")
    other = _make_subscriber(
        db_session, reseller_id=other_reseller.id, suffix="ACTUAL"
    )
    inv_other = _make_invoice(
        db_session, account_id=other.id, balance=Decimal("50.00")
    )
    from app.schemas.billing import PaymentAllocationApply

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("50.00"),
                currency="NGN",
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=inv_other.id, amount=Decimal("50.00")
                    )
                ],
            ),
        )
    assert exc.value.status_code == 400
