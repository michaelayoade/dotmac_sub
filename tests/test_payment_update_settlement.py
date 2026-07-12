"""Editing a payment's status must go through the settlement owner.

Covers F15 in ``docs/audits/BILLING_SOT_AUDIT_2026-07-12.md``.

``Payments.update`` used to blind-``setattr`` the status from the payload. That
skipped the legal-transition table, skipped the ``paid_at`` stamp, and skipped the
``payment_received`` event — while still running the invoice finalize, so the
invoice flipped to paid anyway.

The ``paid_at`` hole is the dangerous one. ``billing_enforcement_guards`` counts
recent settlements by ``paid_at``; a succeeded payment with a NULL ``paid_at`` is
invisible to it, the recent-payment-volume floor trips, and the health gate blocks
*all* collections suspensions. ``create()`` was fixed for exactly this reason;
``update()`` reopened it.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus, PaymentStatus
from app.schemas.billing import PaymentCreate, PaymentUpdate
from app.services import billing as billing_service


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


def _pending_payment(db_session, account_id, invoice: Invoice, amount: str):
    return billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=account_id,
            invoice_id=invoice.id,
            amount=Decimal(amount),
            currency="NGN",
            status="pending",
        ),
    )


def test_updating_status_to_succeeded_stamps_paid_at(db_session, subscriber):
    """The health gate counts settlements by paid_at. It must never be NULL."""
    inv = _invoice(db_session, subscriber.id, "5000.00", "INV-1")
    payment = _pending_payment(db_session, subscriber.id, inv, "5000.00")
    assert payment.paid_at is None

    billing_service.payments.update(
        db_session, str(payment.id), PaymentUpdate(status=PaymentStatus.succeeded)
    )

    db_session.refresh(payment)
    assert payment.status == PaymentStatus.succeeded
    assert payment.paid_at is not None, (
        "succeeded payment has a NULL paid_at — it is invisible to the "
        "enforcement health gate and will block all collections suspensions"
    )


def test_updating_status_to_succeeded_settles_the_invoice(db_session, subscriber):
    inv = _invoice(db_session, subscriber.id, "5000.00", "INV-2")
    payment = _pending_payment(db_session, subscriber.id, inv, "5000.00")

    billing_service.payments.update(
        db_session, str(payment.id), PaymentUpdate(status=PaymentStatus.succeeded)
    )

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid
    assert inv.balance_due == Decimal("0.00")


def test_update_cannot_resurrect_a_refunded_payment(db_session, subscriber):
    """refunded -> succeeded is forbidden by the transition table.

    mark_status has always refused it. update() allowed it, which would settle an
    invoice from money that had already been given back.
    """
    inv = _invoice(db_session, subscriber.id, "5000.00", "INV-3")
    payment = _pending_payment(db_session, subscriber.id, inv, "5000.00")
    billing_service.payments.mark_status(
        db_session, str(payment.id), PaymentStatus.succeeded
    )
    billing_service.payments.mark_status(
        db_session, str(payment.id), PaymentStatus.refunded
    )
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.refunded

    billing_service.payments.update(
        db_session, str(payment.id), PaymentUpdate(status=PaymentStatus.succeeded)
    )

    db_session.refresh(payment)
    assert payment.status == PaymentStatus.refunded, (
        "a refunded payment was resurrected to succeeded via update()"
    )


def test_update_of_a_non_status_field_leaves_settlement_alone(db_session, subscriber):
    """Editing a memo must not disturb status or paid_at."""
    inv = _invoice(db_session, subscriber.id, "5000.00", "INV-4")
    payment = _pending_payment(db_session, subscriber.id, inv, "5000.00")
    billing_service.payments.mark_status(
        db_session, str(payment.id), PaymentStatus.succeeded
    )
    db_session.refresh(payment)
    paid_at = payment.paid_at

    billing_service.payments.update(
        db_session, str(payment.id), PaymentUpdate(memo="corrected reference")
    )

    db_session.refresh(payment)
    assert payment.memo == "corrected reference"
    assert payment.status == PaymentStatus.succeeded
    assert payment.paid_at == paid_at
