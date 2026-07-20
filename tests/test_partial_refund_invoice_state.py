"""A small refund must not un-pay a large invoice.

F17. ``_recalculate_invoice_totals`` counted only ``succeeded`` payments, so a
payment that had been PARTIALLY refunded contributed ZERO to the invoice's paid
amount. Refunding NGN 500 of a NGN 50,000 payment dropped the invoice from fully
paid to fully unpaid, flipped it to overdue, and sent a customer who had paid into
dunning — and, on a postpaid account, into suspension.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus, PaymentStatus
from app.models.subscriber import Subscriber
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services.billing.payments import Refunds


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
            invoice_id=invoice.id,
            amount=Decimal(amount),
            currency="NGN",
            status="succeeded",
        ),
    )


def test_a_small_refund_does_not_un_pay_the_whole_invoice(db_session):
    """The headline case: NGN 500 back off NGN 50,000 must not owe 50,000 again."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "50000.00")
    payment = _pay(db_session, account, invoice, "50000.00")
    db_session.commit()
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid

    Refunds.process_refund(
        db_session,
        str(payment.id),
        refund_amount=Decimal("500.00"),
        reason="goodwill",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()
    db_session.refresh(invoice)
    db_session.refresh(payment)

    assert payment.status == PaymentStatus.partially_refunded
    assert invoice.balance_due == Decimal("500.00"), (
        f"invoice owes {invoice.balance_due} after a NGN 500 refund — the whole "
        "payment stopped counting, so the customer owes the full invoice again"
    )
    assert invoice.status != InvoiceStatus.paid  # 500 outstanding is real
    assert invoice.status != InvoiceStatus.overdue


def test_the_refunded_portion_is_what_becomes_owing(db_session):
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()

    Refunds.process_refund(
        db_session,
        str(payment.id),
        refund_amount=Decimal("2500.00"),
        reason="partial",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.balance_due == Decimal("2500.00")


def test_a_full_refund_still_owes_the_whole_invoice(db_session):
    """The opposite edge must not regress: a full refund DOES un-pay it."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    payment = _pay(db_session, account, invoice, "10000.00")
    db_session.commit()

    Refunds.process_refund(
        db_session,
        str(payment.id),
        reason="full",
        idempotency_key=f"refund-test-{uuid.uuid4().hex}",
    )
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.balance_due == Decimal("10000.00")
    assert invoice.status != InvoiceStatus.paid


def test_an_unrefunded_payment_still_pays_the_invoice_in_full(db_session):
    """The ordinary path must be untouched."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "10000.00")
    _pay(db_session, account, invoice, "10000.00")
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
