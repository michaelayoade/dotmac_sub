"""Service helpers for billing account statements."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel, NotificationStatus
from app.schemas.notification import NotificationCreate
from app.services import display_format
from app.services import notification as notification_service
from app.services.customer_financial_ledger import (
    CustomerFinancialEvent,
    list_customer_financial_events,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatementRange:
    start: datetime
    end: datetime
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class StatementCurrencySummary:
    """One statement balance lane; nominal currencies are never netted."""

    currency: str
    opening_balance: Decimal
    period_delta: Decimal
    closing_balance: Decimal
    opening_display: str
    period_display: str
    closing_display: str


@dataclass(frozen=True, slots=True)
class StatementRow:
    """Template-ready statement row from one canonical financial event."""

    id: str
    occurred_at: datetime
    entry_type: str
    entry_type_label: str
    source: str
    source_label: str
    signed_amount: Decimal
    running_balance: Decimal
    currency: str
    amount_display: str
    running_balance_display: str
    memo: str
    source_url: str | None


def parse_statement_range(
    start_date: str | None,
    end_date: str | None,
    *,
    default_days: int = 30,
) -> StatementRange:
    """Parse user date range and return UTC datetime boundaries."""
    today = datetime.now(UTC).date()
    if end_date:
        end_d = date.fromisoformat(end_date)
    else:
        end_d = today
    if start_date:
        start_d = date.fromisoformat(start_date)
    else:
        start_d = end_d - timedelta(days=default_days)

    if start_d > end_d:
        raise HTTPException(
            status_code=400, detail="start_date must be before end_date"
        )

    start_dt = datetime.combine(start_d, time.min, tzinfo=UTC)
    # Inclusive end date: [start, end+1day)
    end_dt = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=UTC)
    return StatementRange(
        start=start_dt, end=end_dt, start_date=start_d, end_date=end_d
    )


def _signed_amount(entry: CustomerFinancialEvent) -> Decimal:
    return entry.signed_amount


def _event_source_url(entry: CustomerFinancialEvent) -> str | None:
    """Link an event to its authoritative document when one has an admin page."""

    event_type, _, event_id = entry.id.partition(":")
    if not event_id:
        return None
    if event_type == "invoice":
        return f"/admin/billing/invoices/{event_id}"
    if event_type == "payment":
        return f"/admin/billing/payments/{event_id}"
    if event_type == "invoice-writeoff":
        invoice_id = getattr(entry.raw, "invoice_id", None)
        if invoice_id:
            return f"/admin/billing/invoices/{invoice_id}"
    return None


def _amounts_by_currency(
    entries: list[CustomerFinancialEvent],
) -> dict[str, Decimal]:
    amounts: dict[str, Decimal] = {}
    for entry in entries:
        currency = display_format.currency_code(entry.currency)
        amounts[currency] = amounts.get(currency, Decimal("0.00")) + _signed_amount(
            entry
        )
    return amounts


def _statement_entries(
    db: Session,
    *,
    account_id: UUID,
    date_range: StatementRange,
) -> list[CustomerFinancialEvent]:
    return list_customer_financial_events(
        db,
        account_id,
        start=date_range.start,
        end=date_range.end,
        currency=None,
    )


def build_account_statement(
    db: Session,
    *,
    account_id: UUID,
    date_range: StatementRange,
) -> dict[str, Any]:
    """Build a currency-safe statement projection for an account and date range."""
    default_currency = display_format.default_currency(db)
    opening_entries = list_customer_financial_events(
        db,
        account_id,
        end=date_range.start,
        currency=None,
    )
    entries = _statement_entries(db, account_id=account_id, date_range=date_range)
    opening_by_currency = _amounts_by_currency(opening_entries)
    period_by_currency = _amounts_by_currency(entries)
    currencies = sorted(set(opening_by_currency) | set(period_by_currency))
    if not currencies:
        currencies = [default_currency]

    summaries = tuple(
        StatementCurrencySummary(
            currency=currency,
            opening_balance=(
                opening := opening_by_currency.get(currency, Decimal("0.00"))
            ),
            period_delta=(period := period_by_currency.get(currency, Decimal("0.00"))),
            closing_balance=(closing := opening + period),
            opening_display=display_format.format_currency_amount(opening, currency),
            period_display=display_format.format_currency_amount(period, currency),
            closing_display=display_format.format_currency_amount(closing, currency),
        )
        for currency in currencies
    )

    rows: list[StatementRow] = []
    running_by_currency = dict(opening_by_currency)
    for entry in entries:
        signed = _signed_amount(entry)
        currency = display_format.currency_code(entry.currency)
        running_balance = running_by_currency.get(currency, Decimal("0.00")) + signed
        running_by_currency[currency] = running_balance
        source = entry.source.value
        entry_type = entry.entry_type.value
        rows.append(
            StatementRow(
                id=entry.id,
                occurred_at=entry.occurred_at,
                entry_type=entry_type,
                entry_type_label=entry_type.replace("_", " ").title(),
                source=source,
                source_label=source.replace("_", " ").title(),
                signed_amount=signed,
                running_balance=running_balance,
                currency=currency,
                amount_display=display_format.format_currency_amount(signed, currency),
                running_balance_display=display_format.format_currency_amount(
                    running_balance, currency
                ),
                memo=entry.memo,
                source_url=_event_source_url(entry),
            )
        )

    return {
        "rows": rows,
        "summaries": summaries,
        "has_multiple_currencies": len(summaries) > 1,
    }


def render_statement_csv(
    *,
    account_label: str,
    account_id: UUID,
    date_range: StatementRange,
    statement: dict[str, Any],
) -> str:
    """Render statement payload into CSV text."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Account", account_label])
    writer.writerow(["Account ID", str(account_id)])
    writer.writerow(
        [
            "Period",
            f"{date_range.start_date.isoformat()} to {date_range.end_date.isoformat()}",
        ]
    )
    writer.writerow(
        ["Currency", "Opening Balance", "Period Activity", "Closing Balance"]
    )
    for summary in statement["summaries"]:
        writer.writerow(
            [
                summary.currency,
                f"{summary.opening_balance:.2f}",
                f"{summary.period_delta:.2f}",
                f"{summary.closing_balance:.2f}",
            ]
        )
    writer.writerow([])
    writer.writerow(
        [
            "Date",
            "Type",
            "Source",
            "Amount",
            "Running Balance",
            "Currency",
            "Memo",
            "Source URL",
        ]
    )

    for row in statement["rows"]:
        writer.writerow(
            [
                row.occurred_at.date().isoformat(),
                row.entry_type,
                row.source,
                f"{row.signed_amount:.2f}",
                f"{row.running_balance:.2f}",
                row.currency,
                row.memo,
                row.source_url or "",
            ]
        )

    return output.getvalue()


def account_statement_label(account) -> str:
    category_value = getattr(getattr(account, "category", None), "value", None)
    return (
        account.account_number
        or (account.company_name if category_value == "business" else "")
        or f"Account {str(account.id)[:8]}"
    )


def render_account_statement_csv(
    *,
    account,
    account_id: UUID,
    date_range: StatementRange,
    statement: dict[str, Any],
) -> tuple[str, str]:
    account_label = account_statement_label(account)
    content = render_statement_csv(
        account_label=account_label,
        account_id=account_id,
        date_range=date_range,
        statement=statement,
    )
    filename = (
        f"statement_{account_label.replace(' ', '_')}_"
        f"{date_range.start_date.isoformat()}_{date_range.end_date.isoformat()}.csv"
    )
    return content, filename


def queue_account_statement_email(
    db: Session,
    *,
    account,
    date_range: StatementRange,
    statement: dict[str, Any],
    recipient_email: str | None,
) -> str:
    to_email = (recipient_email or account.email or "").strip()
    if not to_email:
        raise HTTPException(
            status_code=400, detail="No recipient email set for this account"
        )
    balance_lines = "\n".join(
        f"{summary.currency}: opening {summary.opening_balance:.2f}, "
        f"activity {summary.period_delta:.2f}, closing {summary.closing_balance:.2f}"
        for summary in statement["summaries"]
    )
    notification_service.notifications.create_customer_notification(
        db,
        NotificationCreate(
            channel=NotificationChannel.email,
            recipient=to_email,
            status=NotificationStatus.queued,
            subject=f"Account statement ({date_range.start_date.isoformat()} - {date_range.end_date.isoformat()})",
            body=(
                "Your account statement is ready.\n\n"
                f"Period: {date_range.start_date.isoformat()} to {date_range.end_date.isoformat()}\n"
                f"Balances by currency:\n{balance_lines}\n"
                f"Transactions: {len(statement['rows'])}\n"
            ),
        ),
    )
    return to_email


def build_and_queue_account_statement_email(
    db: Session,
    *,
    account,
    account_id: UUID,
    start_date: str | None,
    end_date: str | None,
    recipient_email: str | None,
) -> StatementRange:
    date_range = parse_statement_range(start_date, end_date)
    statement = build_account_statement(
        db,
        account_id=account_id,
        date_range=date_range,
    )
    queue_account_statement_email(
        db,
        account=account,
        date_range=date_range,
        statement=statement,
        recipient_email=recipient_email,
    )
    return date_range
