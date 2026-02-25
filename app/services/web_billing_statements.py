"""Service helpers for billing account statements."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.billing import LedgerEntry, LedgerEntryType


@dataclass(frozen=True)
class StatementRange:
    start: datetime
    end: datetime
    start_date: date
    end_date: date


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
        raise HTTPException(status_code=400, detail="start_date must be before end_date")

    start_dt = datetime.combine(start_d, time.min, tzinfo=UTC)
    # Inclusive end date: [start, end+1day)
    end_dt = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=UTC)
    return StatementRange(start=start_dt, end=end_dt, start_date=start_d, end_date=end_d)


def _signed_amount(entry: LedgerEntry) -> Decimal:
    amount = Decimal(str(entry.amount or 0))
    return amount if entry.entry_type == LedgerEntryType.credit else -amount


def _base_ledger_query(db: Session, account_id: UUID):
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.account_id == account_id)
        .filter(LedgerEntry.is_active.is_(True))
    )


def build_account_statement(
    db: Session,
    *,
    account_id: UUID,
    date_range: StatementRange,
) -> dict[str, Any]:
    """Build statement payload for an account and date range."""
    opening_entries = (
        _base_ledger_query(db, account_id)
        .filter(LedgerEntry.created_at < date_range.start)
        .order_by(LedgerEntry.created_at.asc())
        .all()
    )
    opening_balance = sum((_signed_amount(e) for e in opening_entries), Decimal("0.00"))

    entries = (
        _base_ledger_query(db, account_id)
        .filter(
            and_(
                LedgerEntry.created_at >= date_range.start,
                LedgerEntry.created_at < date_range.end,
            )
        )
        .order_by(LedgerEntry.created_at.asc())
        .all()
    )
    period_delta = sum((_signed_amount(e) for e in entries), Decimal("0.00"))
    closing_balance = opening_balance + period_delta

    rows: list[dict[str, Any]] = []
    running_balance = opening_balance
    for entry in entries:
        signed = _signed_amount(entry)
        running_balance += signed
        rows.append(
            {
                "entry": entry,
                "signed_amount": signed,
                "running_balance": running_balance,
            }
        )

    return {
        "rows": rows,
        "opening_balance": opening_balance,
        "period_delta": period_delta,
        "closing_balance": closing_balance,
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
    writer.writerow(["Period", f"{date_range.start_date.isoformat()} to {date_range.end_date.isoformat()}"])
    writer.writerow(["Opening Balance", f"{statement['opening_balance']:.2f}"])
    writer.writerow(["Closing Balance", f"{statement['closing_balance']:.2f}"])
    writer.writerow([])
    writer.writerow(["Date", "Type", "Source", "Amount", "Running Balance", "Memo"])

    for row in statement["rows"]:
        entry = row["entry"]
        writer.writerow(
            [
                entry.created_at.date().isoformat() if entry.created_at else "",
                entry.entry_type.value if entry.entry_type else "",
                entry.source.value if entry.source else "",
                f"{row['signed_amount']:.2f}",
                f"{row['running_balance']:.2f}",
                entry.memo or "",
            ]
        )

    return output.getvalue()
