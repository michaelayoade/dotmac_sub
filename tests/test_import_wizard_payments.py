"""Imported payments must settle invoices like any other payment.

Covers F19 in ``docs/audits/BILLING_SOT_AUDIT_2026-07-12.md``.

The import wizard used to construct a ``Payment`` row directly, defaulting to
``status=succeeded`` with no ``paid_at``, no ``PaymentAllocation``, no
``LedgerEntry`` and no invoice recalculation. The cash landed as an orphan row:
the customer's invoices stayed open, ``get_account_credit_balance`` never saw the
money, and dunning kept chasing them for a debt they had already paid.

Splynx-mirrored payments are deliberately NOT this: they are mirrored, not posted,
and carry no local allocation or ledger entry by design. That distinction is why a
naive "payment with no allocation" query reports ~3,100 false orphans.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    PaymentAllocation,
    PaymentStatus,
)
from app.services.billing._common import get_account_credit_balance
from app.services.web_system_import_wizard import _persist_row, _validate_rows


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


def _import_payment(db_session, account_id, amount: str, **kw):
    """Drive the real import path: validate the CSV row, then persist it."""
    rows = [
        {
            "account_id": str(account_id),
            "amount": amount,
            "currency": "NGN",
            "external_id": f"test-import-{uuid.uuid4()}",
            **kw,
        }
    ]
    valid, errors = _validate_rows("payments", rows)
    assert not errors, errors
    payment = _persist_row(db_session, "payments", valid[0], source_name="test-import")
    db_session.commit()
    db_session.refresh(payment)
    return payment


def test_imported_payment_settles_the_open_invoice(db_session, subscriber):
    inv = _invoice(db_session, subscriber.id, "7500.00", "INV-IMP-1")

    _import_payment(db_session, subscriber.id, "7500.00")

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid, (
        "imported cash left the invoice open — the customer will be dunned for "
        "money they already paid"
    )
    assert inv.balance_due == Decimal("0.00")


def test_imported_payment_posts_allocation_and_ledger(db_session, subscriber):
    inv = _invoice(db_session, subscriber.id, "7500.00", "INV-IMP-2")

    payment = _import_payment(db_session, subscriber.id, "7500.00")

    allocs = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .all()
    )
    entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.is_active.is_(True))
        .all()
    )
    assert len(allocs) == 1
    assert str(allocs[0].invoice_id) == str(inv.id)
    assert entries, "imported payment posted no ledger entry — invisible to credit"


def test_imported_payment_stamps_paid_at(db_session, subscriber):
    """A succeeded payment with a NULL paid_at blinds the enforcement health gate."""
    _invoice(db_session, subscriber.id, "1000.00", "INV-IMP-3")

    payment = _import_payment(db_session, subscriber.id, "1000.00")

    assert payment.status == PaymentStatus.succeeded
    assert payment.paid_at is not None


def test_imported_surplus_becomes_account_credit(db_session, subscriber):
    """Overpayment must reach the customer's credit, not vanish."""
    _invoice(db_session, subscriber.id, "1000.00", "INV-IMP-4")

    _import_payment(db_session, subscriber.id, "2500.00")

    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1500.00"
    )
