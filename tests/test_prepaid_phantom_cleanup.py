"""Tests for the prepaid phantom-invoice cleanup one-off.

Covers each class: void candidate (ledger reversed), legacy-debit overlap
(invoice voided, historical debit preserved), and paid/partial (manual review,
never mutated), plus the targeting exclusions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber, SubscriberStatus
from scripts.one_off.cleanup_prepaid_phantom_invoices import (
    PREPAID_CHARGE_MEMO_PREFIX,
    classify,
    void_invoice,
)


def _subscriber(db, mode=BillingMode.prepaid):
    s = Subscriber(
        first_name="P",
        last_name="P",
        email=f"{uuid.uuid4()}@e.com",
        status=SubscriberStatus.active,
        billing_mode=mode,
    )
    db.add(s)
    db.flush()
    return s


def _phantom(
    db,
    account_id,
    *,
    status=InvoiceStatus.issued,
    balance="100.00",
    splynx_id=None,
    added_by=None,
    meta=None,
    with_debit=True,
    period=True,
):
    now = datetime.now(UTC)
    inv = Invoice(
        account_id=account_id,
        invoice_number=f"INV-{uuid.uuid4()}",
        status=status,
        subtotal=Decimal(balance),
        tax_total=Decimal("0.00"),
        total=Decimal(balance),
        balance_due=Decimal(balance),
        billing_period_start=(now - timedelta(days=5)) if period else None,
        billing_period_end=(now + timedelta(days=25)) if period else None,
        splynx_invoice_id=splynx_id,
        added_by_id=added_by,
        metadata_=meta,
        is_active=True,
    )
    db.add(inv)
    db.flush()
    if with_debit:
        db.add(
            LedgerEntry(
                account_id=account_id,
                invoice_id=inv.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal(balance),
                currency="NGN",
                memo=f"Invoice {inv.invoice_number}",
            )
        )
    db.commit()
    return inv


def _legacy_prepaid_debit(db, account_id, amount="3000.00"):
    e = LedgerEntry(
        account_id=account_id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.adjustment,
        amount=Decimal(amount),
        currency="NGN",
        memo=f"{PREPAID_CHARGE_MEMO_PREFIX}: 30d (X) tok-{uuid.uuid4()}",
    )
    db.add(e)
    db.commit()
    return e


def test_classify_targets_only_runner_phantoms(db_session):
    prepaid = _subscriber(db_session, BillingMode.prepaid)
    target = _phantom(db_session, prepaid.id)
    # exclusions
    postpaid = _subscriber(db_session, BillingMode.postpaid)
    _phantom(db_session, postpaid.id)  # postpaid -> excluded
    _phantom(db_session, prepaid.id, added_by=prepaid.id)  # admin/credit -> excluded
    _phantom(db_session, prepaid.id, splynx_id=123)  # migrated -> excluded
    _phantom(
        db_session, prepaid.id, meta={"credit_exception": True}
    )  # marked -> excluded

    groups = classify(db_session)
    ids = {str(i.id) for g in groups.values() for i in g}
    assert str(target.id) in ids
    assert groups["void_candidate"] == [target] or target in groups["void_candidate"]
    # only the one target is picked up
    assert len(ids) == 1


def test_void_candidate_voids_and_reverses_ledger(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid)
    inv = _phantom(db_session, sub.id, balance="100.00")

    groups = classify(db_session)
    assert inv in groups["void_candidate"]
    void_invoice(db_session, inv, datetime.now(UTC))

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.void
    assert inv.balance_due == Decimal("0.00")
    assert inv.metadata_["void_reason"] == "prepaid_phantom_invoice_cleanup"
    assert inv.metadata_["original_status"] == "issued"
    # Append-only reversing credit posted; the original debit remains active.
    credits = (
        db_session.query(LedgerEntry)
        .filter(
            LedgerEntry.invoice_id == inv.id,
            LedgerEntry.entry_type == LedgerEntryType.credit,
        )
        .all()
    )
    assert len(credits) == 1 and credits[0].amount == Decimal("100.00")
    debit = (
        db_session.query(LedgerEntry)
        .filter(
            LedgerEntry.invoice_id == inv.id,
            LedgerEntry.entry_type == LedgerEntryType.debit,
        )
        .one()
    )
    assert debit.is_active is True
    assert credits[0].reversal_of_entry_id == debit.id


def test_legacy_debit_overlap_voids_invoice_preserves_debit(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid)
    inv = _phantom(db_session, sub.id)
    debit = _legacy_prepaid_debit(db_session, sub.id)

    groups = classify(db_session)
    assert inv in groups["legacy_debit_overlap"]
    assert inv not in groups["void_candidate"]

    void_invoice(db_session, inv, datetime.now(UTC))
    db_session.refresh(inv)
    db_session.refresh(debit)
    assert inv.status == InvoiceStatus.void
    # the historical debit is untouched (invoice_id is NULL, not reversed by void)
    assert debit.is_active is True


def test_paid_phantom_is_manual_review_not_mutated(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid)
    inv = _phantom(
        db_session, sub.id, status=InvoiceStatus.paid, balance="0.00", with_debit=False
    )

    groups = classify(db_session)
    assert inv in groups["manual_review"]
    assert inv not in groups["void_candidate"]
    assert inv not in groups["legacy_debit_overlap"]
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid  # untouched


def test_partial_phantom_is_manual_review(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid)
    inv = _phantom(
        db_session, sub.id, status=InvoiceStatus.partially_paid, balance="50.00"
    )
    groups = classify(db_session)
    assert inv in groups["manual_review"]
