from __future__ import annotations

from datetime import UTC, date, datetime
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
from app.models.splynx_transaction import SplynxBillingTransaction
from app.services.customer_financial_ledger import (
    calculate_customer_balance,
    list_customer_financial_events,
)


def test_canonical_ledger_uses_real_legacy_and_post_cutover_documents(
    db_session, subscriber
):
    db_session.add_all(
        [
            SplynxBillingTransaction(
                splynx_transaction_id=1,
                splynx_customer_id=1001,
                subscriber_id=subscriber.id,
                entry_type="credit",
                amount=Decimal("100.00"),
                description="Legacy payment",
                transaction_date=date(2026, 3, 1),
            ),
            SplynxBillingTransaction(
                splynx_transaction_id=2,
                splynx_customer_id=1001,
                subscriber_id=subscriber.id,
                entry_type="debit",
                amount=Decimal("40.00"),
                description="Legacy service",
                transaction_date=date(2026, 3, 2),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("999.00"),
                currency="NGN",
                memo="Prepaid opening balance @ cutover",
                effective_date=datetime(2026, 3, 15, tzinfo=UTC),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("888.00"),
                currency="NGN",
                memo="Correction: remove overcredit",
                effective_date=datetime(2026, 6, 29, tzinfo=UTC),
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("25.00"),
                refunded_amount=Decimal("0.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                paid_at=datetime(2026, 6, 20, tzinfo=UTC),
                memo="Top-up",
            ),
            Invoice(
                account_id=subscriber.id,
                status=InvoiceStatus.issued,
                total=Decimal("10.00"),
                balance_due=Decimal("10.00"),
                currency="NGN",
                issued_at=datetime(2026, 6, 21, tzinfo=UTC),
                memo="Service charge",
                is_proforma=False,
            ),
        ]
    )
    db_session.commit()

    events = list_customer_financial_events(db_session, subscriber.id, currency=None)
    assert [event.memo for event in events] == [
        "Legacy payment",
        "Legacy service",
        "Top-up",
        "Service charge",
    ]
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("75.00")


def test_canonical_ledger_includes_native_real_adjustments_only(db_session, subscriber):
    db_session.add_all(
        [
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("120.00"),
                currency="NGN",
                memo="Approved billing adjustment",
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("50.00"),
                currency="NGN",
                memo="Data repair 2026-06-29: cleanup",
            ),
        ]
    )
    db_session.commit()

    events = list_customer_financial_events(db_session, subscriber.id, currency=None)
    assert [event.memo for event in events] == ["Approved billing adjustment"]
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("120.00")
