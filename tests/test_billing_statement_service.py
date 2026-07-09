from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.splynx_transaction import SplynxBillingTransaction
from app.schemas.billing import LedgerEntryCreate
from app.services import billing as billing_service
from app.services import web_billing_statements as statements_service


def _create_entry(
    db_session,
    account_id,
    *,
    entry_type: LedgerEntryType,
    amount: str,
    created_at: datetime,
):
    entry = billing_service.ledger_entries.create(
        db_session,
        LedgerEntryCreate(
            account_id=account_id,
            entry_type=entry_type,
            source=LedgerSource.other,
            amount=Decimal(amount),
            currency="NGN",
            memo="test",
        ),
    )
    entry.created_at = created_at
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def test_build_account_statement_calculates_opening_and_closing_balances(
    db_session, subscriber
):
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="100.00",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="20.00",
        created_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="10.00",
        created_at=datetime(2026, 2, 2, tzinfo=UTC),
    )
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="5.00",
        created_at=datetime(2026, 2, 5, tzinfo=UTC),
    )

    date_range = statements_service.parse_statement_range("2026-02-01", "2026-02-28")
    statement = statements_service.build_account_statement(
        db_session,
        account_id=subscriber.id,
        date_range=date_range,
    )

    assert statement["opening_balance"] == Decimal("-80.00")
    assert statement["period_delta"] == Decimal("5.00")
    assert statement["closing_balance"] == Decimal("-75.00")
    assert len(statement["rows"]) == 2


def test_render_statement_csv_includes_balances_and_rows(db_session, subscriber):
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="50.00",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    date_range = statements_service.parse_statement_range("2026-02-01", "2026-02-28")
    statement = statements_service.build_account_statement(
        db_session,
        account_id=subscriber.id,
        date_range=date_range,
    )

    csv_text = statements_service.render_statement_csv(
        account_label="ACC-001",
        account_id=subscriber.id,
        date_range=date_range,
        statement=statement,
    )

    assert "Opening Balance" in csv_text
    assert "Closing Balance" in csv_text
    assert "2026-02-01 to 2026-02-28" in csv_text
    assert "50.00" in csv_text


def test_statement_uses_legacy_mirror_and_excludes_internal_rows(
    db_session, subscriber
):
    subscriber.splynx_customer_id = 12345
    db_session.add(
        SplynxBillingTransaction(
            splynx_transaction_id=9001,
            splynx_customer_id=12345,
            subscriber_id=subscriber.id,
            entry_type="credit",
            amount=Decimal("1000.00"),
            category_name="Payment",
            description="Bank transfer",
            transaction_date=date(2026, 3, 1),
            deleted=False,
        )
    )
    db_session.add(
        SplynxBillingTransaction(
            splynx_transaction_id=9002,
            splynx_customer_id=12345,
            subscriber_id=subscriber.id,
            entry_type="debit",
            amount=Decimal("250.00"),
            category_name="Internet",
            description="Monthly service",
            transaction_date=date(2026, 3, 2),
            deleted=False,
        )
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("500.00"),
            currency="NGN",
            memo="Prepaid opening balance @ cutover",
            created_at=datetime(2026, 6, 16, tzinfo=UTC),
            is_active=True,
        )
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            amount=Decimal("100.00"),
            currency="NGN",
            memo="Correction: cutover reconstructed balance true-up",
            created_at=datetime(2026, 7, 9, tzinfo=UTC),
            is_active=True,
        )
    )
    db_session.commit()

    date_range = statements_service.parse_statement_range("2026-03-01", "2026-07-31")
    statement = statements_service.build_account_statement(
        db_session,
        account_id=subscriber.id,
        date_range=date_range,
    )
    csv_text = statements_service.render_statement_csv(
        account_label="ACC-001",
        account_id=subscriber.id,
        date_range=date_range,
        statement=statement,
    )

    assert statement["closing_balance"] == Decimal("750.00")
    assert "Bank transfer" in csv_text
    assert "Monthly service" in csv_text
    assert "Prepaid opening balance" not in csv_text
    assert "cutover reconstructed" not in csv_text


def test_build_account_statement_excludes_internal_repair_entries(
    db_session, subscriber
):
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="100.00",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    internal = _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="75.00",
        created_at=datetime(2026, 2, 2, tzinfo=UTC),
    )
    internal.memo = "Correction: remove system overcredit"
    db_session.add(internal)
    db_session.commit()

    date_range = statements_service.parse_statement_range("2026-02-01", "2026-02-28")
    statement = statements_service.build_account_statement(
        db_session,
        account_id=subscriber.id,
        date_range=date_range,
    )

    assert statement["period_delta"] == Decimal("100.00")
    assert len(statement["rows"]) == 1
    assert statement["rows"][0]["entry"].memo == "test"
