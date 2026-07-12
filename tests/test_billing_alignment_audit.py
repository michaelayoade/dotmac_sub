"""Regression coverage for the read-only billing alignment harness."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.services.customer_financial_ledger import (
    LEGACY_LEDGER_CUTOVER,
    PAYMENT_ACTIVITY_AT,
    SERVICE_ACTIVITY_AT,
    calculate_customer_balance,
)
from scripts.one_off.billing_alignment_audit import (
    _batch_customer_positions,
    _configure_read_only_session,
    d1_double_swings,
)


def test_d1_detector_finds_legacy_corrupted_pair(db_session, subscriber):
    original = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("2500.00"),
        currency="NGN",
        memo="Top-up",
        is_active=False,
    )
    db_session.add(original)
    db_session.flush()
    reversal = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.payment,
        amount=Decimal("2500.00"),
        currency="NGN",
        memo=f"Reversal of ledger entry {original.id}",
    )
    db_session.add(reversal)
    db_session.commit()

    finding = d1_double_swings(db_session)

    assert finding.count == 1
    assert finding.amount == Decimal("2500.00")
    assert finding.rows[0]["original_id"] == str(original.id)
    assert finding.rows[0]["balance_affecting"] is True


def test_batch_position_matches_canonical_native_balance(db_session, subscriber):
    db_session.add_all(
        [
            Payment(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
            ),
            Invoice(
                account_id=subscriber.id,
                invoice_number="ALIGN-BATCH-1",
                status=InvoiceStatus.issued,
                subtotal=Decimal("30.00"),
                total=Decimal("30.00"),
                balance_due=Decimal("30.00"),
                currency="NGN",
                is_proforma=False,
            ),
            CreditNote(
                account_id=subscriber.id,
                credit_number="ALIGN-CN-1",
                status=CreditNoteStatus.issued,
                subtotal=Decimal("5.00"),
                total=Decimal("5.00"),
                currency="NGN",
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("10.00"),
                currency="NGN",
                memo="Approved manual adjustment",
            ),
        ]
    )
    db_session.commit()

    expected = calculate_customer_balance(db_session, str(subscriber.id))
    actual = _batch_customer_positions(db_session, [subscriber.id], currency="NGN")

    assert expected == Decimal("65.00")
    assert actual[(str(subscriber.id), "NGN")] == expected


def test_batch_position_uses_constant_query_count(db_session, subscriber):
    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", count_statement)
    try:
        _batch_customer_positions(db_session, [subscriber.id], currency="NGN")
    finally:
        event.remove(bind, "before_cursor_execute", count_statement)

    # Mirror discovery plus payments, allocations, invoices, credit notes and
    # operational ledger. The count is per batch, not per account.
    assert statements <= 6


def test_batch_position_matches_canonical_migrated_balance(db_session, subscriber):
    db_session.add(
        SplynxBillingTransaction(
            splynx_transaction_id=900001,
            splynx_customer_id=900001,
            subscriber_id=subscriber.id,
            entry_type="credit",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 3, 1),
            deleted=False,
        )
    )
    db_session.add_all(
        [
            # Pre-window native documents are already represented by the
            # mirror and must not be counted a second time.
            Payment(
                account_id=subscriber.id,
                amount=Decimal("50.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                created_at=PAYMENT_ACTIVITY_AT - timedelta(days=1),
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("20.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                created_at=PAYMENT_ACTIVITY_AT + timedelta(days=1),
            ),
            Invoice(
                account_id=subscriber.id,
                invoice_number="ALIGN-MIRROR-OLD",
                status=InvoiceStatus.issued,
                subtotal=Decimal("30.00"),
                total=Decimal("30.00"),
                balance_due=Decimal("30.00"),
                currency="NGN",
                is_proforma=False,
                created_at=SERVICE_ACTIVITY_AT - timedelta(days=1),
            ),
            Invoice(
                account_id=subscriber.id,
                invoice_number="ALIGN-MIRROR-NEW",
                status=InvoiceStatus.issued,
                subtotal=Decimal("10.00"),
                total=Decimal("10.00"),
                balance_due=Decimal("10.00"),
                currency="NGN",
                is_proforma=False,
                created_at=SERVICE_ACTIVITY_AT + timedelta(days=1),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("5.00"),
                currency="NGN",
                memo="Old imported adjustment",
                effective_date=LEGACY_LEDGER_CUTOVER - timedelta(days=1),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("2.00"),
                currency="NGN",
                memo="New approved adjustment",
                effective_date=LEGACY_LEDGER_CUTOVER + timedelta(days=1),
            ),
        ]
    )
    db_session.commit()

    expected = calculate_customer_balance(db_session, str(subscriber.id))
    actual = _batch_customer_positions(db_session, [subscriber.id], currency="NGN")

    assert expected == Decimal("108.00")
    assert actual[(str(subscriber.id), "NGN")] == expected


def test_batch_position_preserves_per_currency_balances(db_session, subscriber):
    db_session.add_all(
        [
            Payment(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("20.00"),
                currency="USD",
                status=PaymentStatus.succeeded,
            ),
        ]
    )
    db_session.commit()

    positions = _batch_customer_positions(db_session, [subscriber.id], currency=None)

    assert positions[(str(subscriber.id), "NGN")] == Decimal("100.00")
    assert positions[(str(subscriber.id), "USD")] == Decimal("20.00")


def test_postgresql_primary_is_refused_by_default():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )
    db.scalar.return_value = False

    with pytest.raises(RuntimeError, match="Refusing to run"):
        _configure_read_only_session(
            db, statement_timeout_ms=10000, allow_primary=False
        )

    assert db.execute.call_count == 2


def test_postgresql_replica_is_allowed():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )
    db.scalar.return_value = True

    _configure_read_only_session(db, statement_timeout_ms=10000, allow_primary=False)

    assert db.execute.call_count == 2
