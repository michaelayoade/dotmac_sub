"""A refund of X must move the balance by exactly X — on every path.

There were three refund paths and three different money outcomes for the same
action:

    Refunds.refund_payment (API)     -2X   posted a refund ledger entry AND set
                                            refunded_amount; the balance reader
                                            counted BOTH
    Refunds.reverse_payment (chargeback) -2X   posted -X and dropped the payment
                                            out of the counted set, so the +X
                                            vanished too
    Refund button (admin UI)          0X   mark_status(refunded) set no
                                            refunded_amount and posted no ledger
                                            entry: cash left, the customer kept a
                                            phantom credit

On a prepaid account, -2X is a false under-funding: resolve_prepaid_funding sees
`funded=False` and suspends a customer we had just refunded.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerSource,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services.billing.payments import Refunds
from app.services.customer_financial_ledger import calculate_customer_balance


def _account(db) -> Subscriber:
    sub = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _invoice(db, account, total: str) -> Invoice:
    inv = Invoice(
        account_id=account.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        status=InvoiceStatus.issued,
        total=Decimal(total),
        balance_due=Decimal(total),
        currency="NGN",
        is_proforma=False,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _pay(db, account, invoice, amount: str):
    return billing_service.payments.create(
        db,
        PaymentCreate(
            account_id=account.id,
            invoice_id=invoice.id if invoice else None,
            amount=Decimal(amount),
            currency="NGN",
            status="succeeded",
        ),
    )


def _balance(db, account) -> Decimal:
    return calculate_customer_balance(db, str(account.id))


def test_a_full_refund_moves_the_balance_by_exactly_the_refund(db_session):
    """It moved by 2x: refunded_amount AND the refund ledger entry were counted."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()

    before = _balance(db_session, account)

    Refunds.process_refund(
        db_session,
        str(payment.id),
        reason="test",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()

    after = _balance(db_session, account)
    assert before - after == Decimal("10000.00"), (
        f"balance moved by {before - after}, expected 10000.00 — the refund was "
        "counted twice (refunded_amount AND the refund ledger entry)"
    )


def test_a_partial_refund_moves_the_balance_by_exactly_the_refund(db_session):
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()

    before = _balance(db_session, account)

    Refunds.process_refund(
        db_session,
        str(payment.id),
        refund_amount=Decimal("2500.00"),
        reason="partial",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()

    after = _balance(db_session, account)
    assert before - after == Decimal("2500.00")
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.partially_refunded


def test_a_chargeback_moves_the_balance_by_exactly_the_payment(db_session):
    """reverse_payment: the money never really arrived."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()

    before = _balance(db_session, account)

    Refunds.reverse_payment(
        db_session,
        str(payment.id),
        reason="chargeback",
        idempotency_key=f"reversal-test-{uuid.uuid4().hex}",
    )
    db_session.commit()

    after = _balance(db_session, account)
    assert before - after == Decimal("10000.00"), (
        f"balance moved by {before - after}, expected 10000.00"
    )
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.reversed


def test_direct_refund_status_is_rejected_and_owner_moves_exact_money(db_session):
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()

    before = _balance(db_session, account)

    with pytest.raises(Exception):
        billing_service.payments.mark_status(
            db_session, str(payment.id), PaymentStatus.refunded
        )
    Refunds.process_refund(
        db_session,
        str(payment.id),
        reason="confirmed outside Sub",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()
    db_session.refresh(payment)

    after = _balance(db_session, account)
    assert before - after == Decimal("10000.00"), (
        "the refund button moved no money — the customer keeps a phantom credit "
        "for cash that already left the business"
    )
    assert payment.refunded_amount == Decimal("10000.00")
    refund_entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.source == LedgerSource.refund)
        .all()
    )
    assert refund_entries, "no refund ledger entry was posted"


def test_a_full_refund_soft_deletes_the_allocation_with_its_ledger_credit(db_session):
    """It hard-deleted the allocation and orphaned the ledger credit."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()

    Refunds.process_refund(
        db_session,
        str(payment.id),
        reason="test",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()

    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert allocations, "the allocation row was destroyed — the refund is unauditable"
    assert all(not a.is_active for a in allocations)

    live_credits = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.source == LedgerSource.payment)
        .filter(LedgerEntry.is_active.is_(True))
        .all()
    )
    assert not live_credits, (
        "the payment's ledger credit outlived the allocation that justified it"
    )


def test_a_full_refund_reopens_the_invoice(db_session):
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid

    Refunds.process_refund(
        db_session,
        str(payment.id),
        reason="test",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.status != InvoiceStatus.paid
    assert invoice.balance_due == Decimal("10000.00")


def test_refunding_more_than_the_payment_is_refused(db_session):
    account = _account(db_session)
    payment = _pay(db_session, account, None, "5000.00")
    db_session.commit()

    with pytest.raises(Exception):
        Refunds.process_refund(
            db_session,
            str(payment.id),
            refund_amount=Decimal("9000.00"),
            idempotency_key=f"refund-test-{uuid.uuid4().hex}",
        )
