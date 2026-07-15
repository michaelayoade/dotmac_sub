"""Moving a payment between invoices must move all of its effects together.

Covers F3 in ``docs/audits/BILLING_SOT_AUDIT_2026-07-12.md``, which fired twice
in production (2 payments, NGN 60,000).

The admin "edit payment" form used to hard-delete the allocations and write a new
one for the full payment amount. It never touched the ledger, never recapped
against the target's balance, and never recomputed either invoice — so the
released invoice kept reading ``paid`` with no money behind it, while the ledger
still credited the payment to it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance
from app.services.billing.payments import _finalize_invoice_payment_effects


def _invoice(db_session, account_id, total: str, number: str) -> Invoice:
    inv = Invoice(
        account_id=account_id,
        invoice_number=number,
        status=InvoiceStatus.issued,
        total=Decimal(total),
        balance_due=Decimal(total),
        currency="NGN",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    return inv


def _pay(db_session, account_id, invoice: Invoice, amount: str):
    """Build a historical pre-settlement-evidence row for legacy-path tests."""
    payment = Payment(
        account_id=account_id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(payment)
    db_session.flush()
    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=Decimal(amount),
    )
    entry = LedgerEntry(
        account_id=account_id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal(amount),
        currency="NGN",
    )
    db_session.add_all([allocation, entry])
    db_session.flush()
    allocation.ledger_entry_id = entry.id
    _finalize_invoice_payment_effects(db_session, invoice)
    db_session.commit()
    db_session.refresh(payment)
    return payment


def _active_allocs(db_session, payment_id) -> list[PaymentAllocation]:
    return (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment_id)
        .filter(PaymentAllocation.is_active.is_(True))
        .all()
    )


def _active_payment_ledger(db_session, payment_id) -> list[LedgerEntry]:
    return (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment_id)
        .filter(LedgerEntry.source == LedgerSource.payment)
        .filter(LedgerEntry.is_active.is_(True))
        .all()
    )


def test_reallocation_releases_the_old_invoice(db_session, subscriber):
    """The invoice the money left must stop reading as paid."""
    old = _invoice(db_session, subscriber.id, "10000.00", "INV-OLD")
    new = _invoice(db_session, subscriber.id, "10000.00", "INV-NEW")
    payment = _pay(db_session, subscriber.id, old, "10000.00")

    db_session.refresh(old)
    assert old.status == InvoiceStatus.paid
    assert old.balance_due == Decimal("0.00")

    billing_service.payments.reallocate(db_session, str(payment.id), str(new.id))

    db_session.refresh(old)
    db_session.refresh(new)
    assert old.status != InvoiceStatus.paid, (
        "the released invoice still reads as paid with no money behind it"
    )
    assert old.balance_due == Decimal("10000.00")
    assert new.status == InvoiceStatus.paid
    assert new.balance_due == Decimal("0.00")


def test_evidence_backed_payment_reallocation_fails_closed(db_session, subscriber):
    old = _invoice(db_session, subscriber.id, "5000.00", "INV-EVIDENCE-OLD")
    new = _invoice(db_session, subscriber.id, "5000.00", "INV-EVIDENCE-NEW")
    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("5000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )

    with pytest.raises(HTTPException) as blocked:
        billing_service.payments.reallocate(db_session, str(payment.id), str(new.id))
    assert blocked.value.status_code == 409
    assert "reviewed reversal workflow" in blocked.value.detail
    db_session.refresh(old)
    assert old.status == InvoiceStatus.paid


def test_reallocation_moves_the_ledger_credit_with_the_allocation(
    db_session, subscriber
):
    """The ledger must not keep crediting an invoice the payment has left."""
    old = _invoice(db_session, subscriber.id, "10000.00", "INV-OLD")
    new = _invoice(db_session, subscriber.id, "10000.00", "INV-NEW")
    payment = _pay(db_session, subscriber.id, old, "10000.00")

    billing_service.payments.reallocate(db_session, str(payment.id), str(new.id))

    allocs = _active_allocs(db_session, payment.id)
    entries = _active_payment_ledger(db_session, payment.id)

    assert [str(a.invoice_id) for a in allocs] == [str(new.id)]
    # This is the D5 signature: ledger.invoice_id != allocation.invoice_id.
    assert [str(e.invoice_id) for e in entries if e.invoice_id] == [str(new.id)], (
        "ledger still credits the old invoice while the allocation points at the new one"
    )


def test_reallocation_caps_at_the_target_balance_and_credits_the_rest(
    db_session, subscriber
):
    """A NGN10,000 payment must not allocate NGN10,000 to a NGN4,000 invoice."""
    old = _invoice(db_session, subscriber.id, "10000.00", "INV-OLD")
    small = _invoice(db_session, subscriber.id, "4000.00", "INV-SMALL")
    payment = _pay(db_session, subscriber.id, old, "10000.00")

    billing_service.payments.reallocate(db_session, str(payment.id), str(small.id))

    db_session.refresh(small)
    allocs = _active_allocs(db_session, payment.id)
    assert len(allocs) == 1
    assert allocs[0].amount == Decimal("4000.00"), (
        "allocation exceeded the debt it settles"
    )
    assert small.status == InvoiceStatus.paid
    assert small.balance_due == Decimal("0.00")

    # The NGN6,000 the invoice could not absorb belongs to the customer.
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "6000.00"
    )


def test_reallocation_to_the_same_invoice_is_a_no_op(db_session, subscriber):
    inv = _invoice(db_session, subscriber.id, "5000.00", "INV-SAME")
    payment = _pay(db_session, subscriber.id, inv, "5000.00")

    billing_service.payments.reallocate(db_session, str(payment.id), str(inv.id))

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid
    assert inv.balance_due == Decimal("0.00")
    assert len(_active_allocs(db_session, payment.id)) == 1
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_reallocation_rejects_another_accounts_invoice(db_session, subscriber):
    other = Subscriber(
        first_name="Other",
        last_name="Account",
        email="payment-reallocation-other@example.com",
    )
    db_session.add(other)
    db_session.commit()
    old = _invoice(db_session, subscriber.id, "5000.00", "INV-OWNER")
    foreign = _invoice(db_session, other.id, "5000.00", "INV-FOREIGN")
    payment = _pay(db_session, subscriber.id, old, "5000.00")

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.reallocate(
            db_session, str(payment.id), str(foreign.id)
        )

    assert exc.value.status_code == 400
    assert "different account" in exc.value.detail


def test_reallocation_rejects_cross_currency_invoice(db_session, subscriber):
    old = _invoice(db_session, subscriber.id, "5000.00", "INV-NGN")
    foreign_currency = _invoice(db_session, subscriber.id, "5000.00", "INV-USD")
    foreign_currency.currency = "USD"
    db_session.commit()
    payment = _pay(db_session, subscriber.id, old, "5000.00")

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.reallocate(
            db_session, str(payment.id), str(foreign_currency.id)
        )

    assert exc.value.status_code == 400
    assert "Currency" in exc.value.detail


@pytest.mark.parametrize(
    "status", [InvoiceStatus.draft, InvoiceStatus.void, InvoiceStatus.written_off]
)
def test_reallocation_rejects_non_allocatable_invoice(db_session, subscriber, status):
    old = _invoice(db_session, subscriber.id, "5000.00", f"INV-OLD-{status.value}")
    target = _invoice(
        db_session, subscriber.id, "5000.00", f"INV-TARGET-{status.value}"
    )
    target.status = status
    db_session.commit()
    payment = _pay(db_session, subscriber.id, old, "5000.00")

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.reallocate(db_session, str(payment.id), str(target.id))

    assert exc.value.status_code == 400


def test_reallocation_rejects_non_succeeded_payment(db_session, subscriber):
    target = _invoice(db_session, subscriber.id, "5000.00", "INV-PENDING")
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("5000.00"),
        currency="NGN",
        status=PaymentStatus.pending,
    )
    db_session.add(payment)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.reallocate(db_session, str(payment.id), str(target.id))

    assert exc.value.status_code == 409
    assert "succeeded" in exc.value.detail


def test_reallocation_rejects_consolidated_payment(db_session, subscriber):
    target = _invoice(db_session, subscriber.id, "5000.00", "INV-CONSOLIDATED")
    payment = Payment(
        account_id=None,
        amount=Decimal("5000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(payment)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.reallocate(db_session, str(payment.id), str(target.id))

    assert exc.value.status_code == 400
    assert "Consolidated" in exc.value.detail
