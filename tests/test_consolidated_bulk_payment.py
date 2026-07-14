"""A reseller bulk payment must settle the member invoices it pays for.

F18. ``record_bulk_payment`` built a PaymentCreate with no status, which fell back
to the ``default_payment_status`` setting — default "pending".

A pending payment is HALF-APPLIED. The allocation and the ledger credit are written
regardless of status, and any surplus credits ``BillingAccount.balance``
(immediately spendable), but ``_recalculate_invoice_totals`` counts only succeeded
payments. So the reseller's bank transfer credited their balance while every member
invoice stayed unpaid.

The existing web test mocked ``record_bulk_payment`` out entirely, which is exactly
why this survived: nothing ever exercised the money path.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.billing import (
    BillingAccount,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.web_consolidated_billing import record_bulk_payment


def _billing_account(db) -> BillingAccount:
    reseller = Reseller(name=f"R-{uuid.uuid4().hex[:6]}")
    db.add(reseller)
    db.commit()
    db.refresh(reseller)

    ba = BillingAccount(
        reseller_id=reseller.id,
        name=f"BA-{uuid.uuid4().hex[:6]}",
        currency="NGN",
    )
    db.add(ba)
    db.commit()
    db.refresh(ba)
    return ba


def _member_with_invoice(db, ba, total: str) -> Invoice:
    sub = Subscriber(
        first_name="M",
        last_name="Ember",
        email=f"m{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
        reseller_id=ba.reseller_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    inv = Invoice(
        account_id=sub.id,
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


def test_a_recorded_bulk_payment_is_succeeded_not_pending(db_session):
    """A bank transfer an admin has confirmed is cash, not a maybe."""
    ba = _billing_account(db_session)

    payment_id = record_bulk_payment(
        db_session,
        billing_account_id=str(ba.id),
        amount="100000.00",
        memo="bank transfer",
    )
    db_session.commit()

    payment = db_session.get(Payment, payment_id)
    assert payment.status == PaymentStatus.succeeded, (
        "the bulk payment landed as pending — it credits the reseller's balance "
        "but settles none of their member invoices"
    )


def test_a_bulk_payment_settles_the_member_invoices(db_session):
    """The whole point: the money must reach the invoices it paid for."""
    ba = _billing_account(db_session)
    invoice = _member_with_invoice(db_session, ba, "40000.00")

    record_bulk_payment(
        db_session,
        billing_account_id=str(ba.id),
        amount="40000.00",
        memo="bank transfer",
    )
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.status == InvoiceStatus.paid, (
        f"member invoice is {invoice.status.value} after the reseller paid it — "
        "the transfer credited the balance and settled nothing"
    )
    assert invoice.balance_due == Decimal("0.00")
