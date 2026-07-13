"""An import rollback must undo the money, not delete the evidence.

This is a regression WE introduced. F19 routed imported payments through
Payments.create so they would actually settle invoices — which means every
imported payment now has a LedgerEntry and usually a PaymentAllocation beneath it.
The rollback path still hard-deleted the Payment row, and it was written back when
imported payments were childless orphans.

Neither child FK carries an ondelete, so db.delete() either raises IntegrityError
(leaving the batch half-rolled-back and unrecoverable) or orphans a live ledger
credit while the invoice it settled stays paid with balance_due = 0.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Payment,
    PaymentAllocation,
)
from app.models.subscriber import Subscriber
from app.services.billing._common import get_account_credit_balance
from app.services.web_system_import_wizard import _persist_row, _validate_rows


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
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _import_payment(db, account, amount: str) -> Payment:
    rows = [{"account_id": str(account.id), "amount": amount, "currency": "NGN"}]
    valid, errors = _validate_rows("payments", rows)
    assert not errors, errors
    payment = _persist_row(db, "payments", valid[0], source_name="test-import")
    db.commit()
    db.refresh(payment)
    return payment


def _rollback_payment(db, payment: Payment) -> None:
    """Drive the owner path the rollback now uses."""
    from app.services import billing as billing_service

    billing_service.payments.delete(db, str(payment.id))


def test_imported_payment_has_children_that_a_hard_delete_would_orphan(
    db_session,
):
    """Establish the premise: F19 gave imported payments a ledger entry."""
    account = _account(db_session)
    _invoice(db_session, account, "5000.00")

    payment = _import_payment(db_session, account, "5000.00")

    entries = (
        db_session.query(LedgerEntry).filter(LedgerEntry.payment_id == payment.id).all()
    )
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert entries, "imported payment has no ledger entry — F19 did not take effect"
    assert allocations, "imported payment has no allocation"


def test_rollback_reopens_the_invoice_the_import_settled(db_session):
    """The invoice must not stay paid with no money behind it."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "5000.00")

    payment = _import_payment(db_session, account, "5000.00")
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid

    _rollback_payment(db_session, payment)
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.status != InvoiceStatus.paid, (
        "the rollback left the invoice paid with no payment behind it"
    )
    assert invoice.balance_due == Decimal("5000.00")


def test_rollback_removes_the_ledger_credit_it_imported(db_session):
    """A hard delete would orphan this credit; the customer would keep the money."""
    account = _account(db_session)
    # No invoice: the whole payment lands as unallocated account credit.
    payment = _import_payment(db_session, account, "5000.00")
    assert get_account_credit_balance(db_session, str(account.id)) == Decimal("5000.00")

    _rollback_payment(db_session, payment)
    db_session.commit()

    assert get_account_credit_balance(db_session, str(account.id)) == Decimal("0.00"), (
        "the rolled-back import left its ledger credit active — the customer "
        "keeps money that was never really theirs"
    )


def test_rollback_deactivates_the_payment_and_its_allocation_together(db_session):
    account = _account(db_session)
    _invoice(db_session, account, "5000.00")
    payment = _import_payment(db_session, account, "5000.00")

    _rollback_payment(db_session, payment)
    db_session.commit()
    db_session.refresh(payment)

    assert payment.is_active is False
    live_allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .all()
    )
    live_entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.is_active.is_(True))
        .all()
    )
    assert not live_allocations, "allocation survived the rollback"
    assert not live_entries, "ledger credit survived the rollback"


def test_rollback_preserves_the_audit_trail(db_session):
    """Soft-delete, not hard-delete: the import must remain investigable."""
    account = _account(db_session)
    payment = _import_payment(db_session, account, "5000.00")
    payment_id = payment.id

    _rollback_payment(db_session, payment)
    db_session.commit()

    assert db_session.get(Payment, payment_id) is not None, (
        "the payment row was destroyed — a money import that went wrong is now "
        "unauditable"
    )
