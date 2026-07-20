"""A generic import rollback must not delete settled payment evidence.

This is a regression WE introduced. F19 routed imported payments through
Payments.create so they would actually settle invoices — which means every
imported payment now has a LedgerEntry and usually a PaymentAllocation beneath it.
The old rollback path hard-deleted the Payment row, and it was written back when
imported payments were childless orphans. The intermediate soft-delete repair is
also no longer valid once settlement rows structurally own exact evidence: import
rollback must use a separately previewed reversal workflow.

The durable Import Runs workflow now has that batch owner. Generic payment
deletion still fails closed so every caller must enter through the previewed,
confirmed batch contract instead of bypassing its provenance and evidence.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

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
    rows = [
        {
            "account_id": str(account.id),
            "amount": amount,
            "currency": "NGN",
            "external_id": f"test-import-{uuid.uuid4()}",
        }
    ]
    valid, errors = _validate_rows("payments", rows)
    assert not errors, errors
    payment = _persist_row(db, "payments", valid[0], source_name="test-import")
    db.commit()
    db.refresh(payment)
    return payment


def _rollback_payment(db, payment: Payment) -> None:
    """Exercise the generic delete path that the batch owner replaces."""
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


def test_generic_delete_cannot_mutate_settled_import_evidence(db_session):
    """Imported money must enter through the previewed batch owner."""
    account = _account(db_session)
    invoice = _invoice(db_session, account, "5000.00")

    payment = _import_payment(db_session, account, "5000.00")
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid

    with pytest.raises(HTTPException) as blocked:
        _rollback_payment(db_session, payment)
    assert blocked.value.status_code == 409
    db_session.refresh(invoice)

    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")


def test_rejected_rollback_preserves_the_settlement_ledger(db_session):
    account = _account(db_session)
    # No invoice: the whole payment lands as unallocated account credit.
    payment = _import_payment(db_session, account, "5000.00")
    assert get_account_credit_balance(db_session, str(account.id)) == Decimal("5000.00")

    with pytest.raises(HTTPException) as blocked:
        _rollback_payment(db_session, payment)
    assert blocked.value.status_code == 409

    assert get_account_credit_balance(db_session, str(account.id)) == Decimal("5000.00")


def test_rejected_rollback_preserves_payment_and_allocation_evidence(db_session):
    account = _account(db_session)
    _invoice(db_session, account, "5000.00")
    payment = _import_payment(db_session, account, "5000.00")

    with pytest.raises(HTTPException) as blocked:
        _rollback_payment(db_session, payment)
    assert blocked.value.status_code == 409
    db_session.refresh(payment)

    assert payment.is_active is True
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
    assert live_allocations
    assert live_entries


def test_rejected_rollback_preserves_the_audit_trail(db_session):
    account = _account(db_session)
    payment = _import_payment(db_session, account, "5000.00")
    payment_id = payment.id

    with pytest.raises(HTTPException) as blocked:
        _rollback_payment(db_session, payment)
    assert blocked.value.status_code == 409

    assert db_session.get(Payment, payment_id) is not None, (
        "the payment row was destroyed — a money import that went wrong is now "
        "unauditable"
    )
