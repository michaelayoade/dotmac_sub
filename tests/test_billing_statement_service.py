from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.schemas.billing import LedgerEntryCreate
from app.services import billing as billing_service
from app.services import web_billing_statements as statements_service
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


def _create_entry(
    db_session,
    account_id,
    *,
    entry_type: LedgerEntryType,
    amount: str,
    created_at: datetime,
    affects_customer_position: bool = True,
    currency: str = "NGN",
):
    entry = billing_service.ledger_entries.create(
        db_session,
        LedgerEntryCreate(
            account_id=account_id,
            entry_type=entry_type,
            source=LedgerSource.other,
            amount=Decimal(amount),
            currency=currency,
            memo="test",
        ),
        affects_customer_position=affects_customer_position,
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

    summary = statement["summaries"][0]
    assert summary.currency == "NGN"
    assert summary.opening_balance == Decimal("-80.00")
    assert summary.period_delta == Decimal("5.00")
    assert summary.closing_balance == Decimal("-75.00")
    assert len(statement["rows"]) == 2
    assert statement["rows"][0].running_balance == Decimal("-70.00")
    assert statement["rows"][1].running_balance == Decimal("-75.00")


def test_build_account_statement_keeps_currency_balances_separate(
    db_session, subscriber
):
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.credit,
        amount="100.00",
        currency="NGN",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    _create_entry(
        db_session,
        subscriber.id,
        entry_type=LedgerEntryType.debit,
        amount="20.00",
        currency="USD",
        created_at=datetime(2026, 2, 2, tzinfo=UTC),
    )

    date_range = statements_service.parse_statement_range("2026-02-01", "2026-02-28")
    statement = statements_service.build_account_statement(
        db_session,
        account_id=subscriber.id,
        date_range=date_range,
    )

    assert statement["has_multiple_currencies"] is True
    assert {
        summary.currency: summary.closing_balance for summary in statement["summaries"]
    } == {"NGN": Decimal("100.00"), "USD": Decimal("-20.00")}
    assert [row.running_balance_display for row in statement["rows"]] == [
        "NGN 100.00",
        "USD -20.00",
    ]


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

    assert "Currency,Opening Balance,Period Activity,Closing Balance" in csv_text
    assert "2026-02-01 to 2026-02-28" in csv_text
    assert "50.00" in csv_text


def test_statement_row_links_to_its_authoritative_invoice(db_session, subscriber):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-STATEMENT-LINK",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("25.00"),
        balance_due=Decimal("25.00"),
        issued_at=datetime(2026, 2, 4, tzinfo=UTC),
        is_proforma=False,
    )
    db_session.add(invoice)
    db_session.commit()

    statement = statements_service.build_account_statement(
        db_session,
        account_id=subscriber.id,
        date_range=statements_service.parse_statement_range("2026-02-01", "2026-02-28"),
    )

    assert len(statement["rows"]) == 1
    row = statement["rows"][0]
    assert row.source == "invoice"
    assert row.source_url == f"/admin/billing/invoices/{invoice.id}"


def test_statement_ignores_archived_mirror_and_structural_ledger_rows(
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
            affects_customer_position=False,
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
            affects_customer_position=False,
        )
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("750.00"),
        position_at=datetime(2026, 3, 15, tzinfo=UTC),
    )

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

    assert statement["summaries"][0].closing_balance == Decimal("750.00")
    assert "Bank transfer" not in csv_text
    assert "Monthly service" not in csv_text
    assert "Reviewed prepaid opening position" in csv_text
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
        affects_customer_position=False,
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

    assert statement["summaries"][0].period_delta == Decimal("100.00")
    assert len(statement["rows"]) == 1
    assert statement["rows"][0].memo == "test"
