"""Tests for the prepaid phantom-AR cleanup one-off (Item 5)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing._common import get_account_credit_balance
from scripts.one_off.cleanup_prepaid_phantom_ar import CLEANUP_MARKER, run_cleanup


def _subscriber(db, *, mode=BillingMode.prepaid) -> Subscriber:
    sub = Subscriber(
        first_name="P",
        last_name="Q",
        email=f"{uuid.uuid4()}@example.com",
        status=SubscriberStatus.active,
        billing_mode=mode,
    )
    db.add(sub)
    db.flush()
    return sub


def _invoice(db, account, *, status=InvoiceStatus.overdue, amount="100.00") -> Invoice:
    amt = Decimal(amount)
    inv = Invoice(
        account_id=account.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:10]}",
        currency="NGN",
        subtotal=amt,
        total=amt,
        balance_due=amt,
        status=status,
        issued_at=None,
        due_at=None,
    )
    db.add(inv)
    db.flush()
    return inv


def _add_credit(db, account, amount="100.00") -> None:
    payment = Payment(
        account_id=account.id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentStatus.succeeded,
        memo="Top-up",
    )
    db.add(payment)
    db.flush()
    db.add(
        LedgerEntry(
            account_id=account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal(amount),
            currency="NGN",
            memo="Top-up",
        )
    )
    db.flush()


def test_funded_invoice_settled_from_credit(db_session):
    acct = _subscriber(db_session)
    inv = _invoice(db_session, acct, amount="100.00")
    _add_credit(db_session, acct, "100.00")
    db_session.commit()

    result = run_cleanup(db_session, apply=True, unfunded_action="draft")

    assert result["funded_settled"] == 1
    assert result["unfunded_retired"] == 0
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid
    assert inv.balance_due == Decimal("0.00")
    assert (inv.metadata_ or {}).get(CLEANUP_MARKER, {}).get("action") == "settled"
    # Wallet drawn down by exactly the invoice amount.
    assert get_account_credit_balance(
        db_session, str(acct.id), currency="NGN"
    ) == Decimal("0.00")


def test_unfunded_invoice_drafted(db_session):
    acct = _subscriber(db_session)
    inv = _invoice(db_session, acct, amount="100.00")
    db_session.commit()

    result = run_cleanup(db_session, apply=True, unfunded_action="draft")

    assert result["unfunded_retired"] == 1
    assert result["funded_settled"] == 0
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.draft
    assert inv.due_at is None
    assert (inv.metadata_ or {}).get(CLEANUP_MARKER, {}).get("action") == "draft"


def test_unfunded_invoice_void_variant(db_session):
    acct = _subscriber(db_session)
    inv = _invoice(db_session, acct, amount="100.00")
    db_session.commit()

    run_cleanup(db_session, apply=True, unfunded_action="void")

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.void


def test_postpaid_invoice_untouched(db_session):
    acct = _subscriber(db_session, mode=BillingMode.postpaid)
    inv = _invoice(db_session, acct, amount="100.00")
    db_session.commit()

    result = run_cleanup(db_session, apply=True, unfunded_action="draft")

    assert result["accounts"] == 0
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.overdue


def test_dry_run_writes_nothing(db_session):
    acct = _subscriber(db_session)
    inv = _invoice(db_session, acct, amount="100.00")
    db_session.commit()

    result = run_cleanup(db_session, apply=False, unfunded_action="draft")

    assert result["unfunded_retired"] == 1  # projected
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.overdue  # unchanged
    assert not (inv.metadata_ or {}).get(CLEANUP_MARKER)


def test_idempotent_second_run_skips_processed(db_session):
    acct = _subscriber(db_session)
    _invoice(db_session, acct, amount="100.00")
    db_session.commit()

    run_cleanup(db_session, apply=True, unfunded_action="draft")
    # Second run: the drafted row is out of the AR set AND marked → nothing to do.
    result = run_cleanup(db_session, apply=True, unfunded_action="draft")
    assert result["accounts"] == 0
    assert result["unfunded_retired"] == 0


def test_partial_credit_leaves_invoice_as_unfunded(db_session):
    acct = _subscriber(db_session)
    inv = _invoice(db_session, acct, amount="100.00")
    _add_credit(db_session, acct, "40.00")  # not enough → all-or-nothing
    db_session.commit()

    result = run_cleanup(db_session, apply=True, unfunded_action="draft")

    assert result["funded_settled"] == 0
    assert result["unfunded_retired"] == 1
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.draft
    # Credit untouched (nothing was settled).
    assert get_account_credit_balance(
        db_session, str(acct.id), currency="NGN"
    ) == Decimal("40.00")
