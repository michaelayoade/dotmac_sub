"""Service helpers for billing ledger web routes."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.subscriber import Reseller, Subscriber
from app.services import web_billing_customers as web_billing_customers_service
from app.services.common import validate_enum

_CATEGORY_SOURCES: dict[str, tuple[LedgerSource, ...]] = {
    "service": (LedgerSource.invoice,),
    "payment": (LedgerSource.payment,),
    "credit_note": (LedgerSource.credit_note,),
    "adjustment": (LedgerSource.adjustment,),
    "refund": (LedgerSource.refund,),
    "other": (LedgerSource.other,),
}


def build_ledger_entries_data(
    db,
    *,
    customer_ref: str | None,
    entry_type: str | None,
    date_range: str | None = None,
    category: str | None = None,
    partner_id: str | None = None,
    limit: int = 200,
) -> dict[str, object]:
    def _apply_date_range(query):  # type: ignore[no-untyped-def]
        if date_range not in {"today", "week", "month", "quarter", "year"}:
            return query
        now = datetime.now(UTC)
        if date_range == "today":
            start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        elif date_range == "week":
            start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
        elif date_range == "month":
            start = datetime(now.year, now.month, 1, tzinfo=UTC)
        elif date_range == "quarter":
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start = datetime(now.year, quarter_start_month, 1, tzinfo=UTC)
        else:
            start = datetime(now.year, 1, 1, tzinfo=UTC)
        return query.filter(LedgerEntry.created_at >= start)

    account_ids = []
    if customer_ref:
        account_ids = [
            UUID(item["id"])
            for item in web_billing_customers_service.accounts_for_customer(db, customer_ref)
        ]

    entries = []
    selected_partner_id = (partner_id or "").strip() or None
    partner_options = [
        {"id": str(item.id), "name": item.name}
        for item in db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .order_by(Reseller.name.asc())
        .all()
    ]
    if account_ids or not customer_ref:
        query = db.query(LedgerEntry).filter(LedgerEntry.is_active.is_(True))
        if account_ids:
            query = query.filter(LedgerEntry.account_id.in_(account_ids))
        if entry_type:
            query = query.filter(
                LedgerEntry.entry_type == validate_enum(entry_type, LedgerEntryType, "entry_type")
            )
        if selected_partner_id:
            query = query.filter(LedgerEntry.account.has(Subscriber.reseller_id == UUID(selected_partner_id)))
        selected_category = (category or "").strip().lower()
        if selected_category in _CATEGORY_SOURCES:
            query = query.filter(LedgerEntry.source.in_(_CATEGORY_SOURCES[selected_category]))
        query = _apply_date_range(query)
        entries = query.order_by(LedgerEntry.created_at.desc()).limit(limit).offset(0).all()

    credit_entries = [
        entry for entry in entries if getattr(getattr(entry, "entry_type", None), "value", None) == "credit"
    ]
    debit_entries = [
        entry for entry in entries if getattr(getattr(entry, "entry_type", None), "value", None) == "debit"
    ]
    credit_total = sum(float(getattr(entry, "amount", 0) or 0) for entry in credit_entries)
    debit_total = sum(float(getattr(entry, "amount", 0) or 0) for entry in debit_entries)
    ledger_totals = {
        "credit_count": len(credit_entries),
        "credit_total": credit_total,
        "debit_count": len(debit_entries),
        "debit_total": debit_total,
        "net_total": credit_total - debit_total,
    }

    return {
        "entries": entries,
        "ledger_totals": ledger_totals,
        "entry_type": entry_type,
        "customer_ref": customer_ref,
        "date_range": date_range,
        "category": category,
        "selected_partner_id": selected_partner_id,
        "partner_options": partner_options,
    }


def render_ledger_csv(entries: list[LedgerEntry]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "entry_id",
            "account_id",
            "entry_type",
            "source",
            "debit_amount",
            "credit_amount",
            "currency",
            "description",
            "created_at",
        ]
    )
    for entry in entries:
        entry_type = getattr(getattr(entry, "entry_type", None), "value", "") or ""
        amount = Decimal(str(getattr(entry, "amount", 0) or 0))
        writer.writerow(
            [
                str(entry.id),
                str(entry.account_id) if entry.account_id else "",
                entry_type,
                getattr(getattr(entry, "source", None), "value", "") or "",
                f"{amount:.2f}" if entry_type == "debit" else "",
                f"{amount:.2f}" if entry_type == "credit" else "",
                entry.currency or "NGN",
                entry.memo or "",
                entry.created_at.isoformat() if entry.created_at else "",
            ]
        )
    return buffer.getvalue()
