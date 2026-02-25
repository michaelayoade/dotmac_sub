from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import LedgerEntryType, LedgerSource
from app.schemas.billing import LedgerEntryCreate
from app.services import billing as billing_service
from app.services import web_billing_statements as statements_service


def _create_entry(db_session, account_id, *, entry_type: LedgerEntryType, amount: str, created_at: datetime):
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


def test_build_account_statement_calculates_opening_and_closing_balances(db_session, subscriber):
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
