"""Service helpers for invoice PDF cache management page."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoicePdfExport, InvoicePdfExportStatus
from app.models.subscriber import Subscriber
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services.common import parse_date_filter as _parse_date

logger = logging.getLogger(__name__)

def _format_size(size: int) -> str:
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.2f} KB"
    return f"{size} B"


def build_cache_page_state(
    db: Session,
    *,
    date_from: str | None,
    date_to: str | None,
    account_id: str | None,
) -> dict[str, Any]:
    from_dt = _parse_date(date_from)
    to_dt_base = _parse_date(date_to)
    to_dt = (to_dt_base + timedelta(days=1)) if to_dt_base else None

    stats = billing_invoice_pdf_service.get_cache_dashboard_stats(db)

    stmt = (
        select(InvoicePdfExport)
        .join(Invoice, Invoice.id == InvoicePdfExport.invoice_id)
        .where(InvoicePdfExport.status == InvoicePdfExportStatus.completed)
        .order_by(InvoicePdfExport.completed_at.desc())
    )
    if from_dt:
        stmt = stmt.where(InvoicePdfExport.completed_at >= from_dt)
    if to_dt:
        stmt = stmt.where(InvoicePdfExport.completed_at < to_dt)
    if account_id:
        stmt = stmt.where(Invoice.account_id == account_id)

    seen_invoice_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for export in db.scalars(stmt.limit(1000)).all():
        invoice_id = str(export.invoice_id)
        if invoice_id in seen_invoice_ids:
            continue
        seen_invoice_ids.add(invoice_id)
        invoice = export.invoice
        account = invoice.account if invoice else None
        account_name = "Account"
        if account:
            full_name = getattr(account, "full_name", None)
            account_email = getattr(account, "email", None)
            account_name = (full_name or account_email or str(account.id)).strip()
        rows.append(
            {
                "invoice_id": invoice_id,
                "invoice_number": invoice.invoice_number if invoice else invoice_id,
                "account_id": str(invoice.account_id) if invoice else "",
                "account_name": account_name,
                "cached_at": export.completed_at,
                "cache_size_bytes": int(export.file_size_bytes or 0),
                "cache_size_label": _format_size(int(export.file_size_bytes or 0)),
                "export_id": str(export.id),
            }
        )

    # Resolve account ids first. Selecting DISTINCT Subscriber rows breaks on Postgres
    # when Subscriber.metadata is plain JSON because equality is undefined for that type.
    account_ids = db.scalars(
        select(Invoice.account_id)
        .where(Invoice.is_active.is_(True))
        .distinct()
        .limit(300)
    ).all()
    accounts: Sequence[Subscriber] = []
    if account_ids:
        accounts = db.scalars(
            select(Subscriber)
            .where(Subscriber.id.in_(account_ids))
            .order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc(), Subscriber.email.asc())
        ).all()

    return {
        "stats": {
            **stats,
            "cache_size_label": _format_size(int(stats.get("cache_size_bytes") or 0)),
            "avg_generation_label": f"{float(stats.get('avg_generation_seconds') or 0):.2f}s",
        },
        "rows": rows,
        "accounts": accounts,
        "filters": {
            "date_from": date_from or "",
            "date_to": date_to or "",
            "account_id": account_id or "",
        },
    }


def clear_cache_from_form(
    db: Session,
    *,
    mode: str,
    date_from: str | None,
    date_to: str | None,
    account_id: str | None,
) -> dict[str, int]:
    selected_mode = (mode or "all").strip().lower()
    if selected_mode == "all":
        return billing_invoice_pdf_service.clear_cache(db)
    if selected_mode == "date_range":
        from_dt = _parse_date(date_from)
        to_dt_base = _parse_date(date_to)
        to_dt = (to_dt_base + timedelta(days=1)) if to_dt_base else None
        return billing_invoice_pdf_service.clear_cache(db, date_from=from_dt, date_to=to_dt)
    if selected_mode == "account":
        if not account_id:
            return {"invalidated": 0, "bytes_cleared": 0}
        return billing_invoice_pdf_service.clear_cache(db, account_id=account_id)
    return {"invalidated": 0, "bytes_cleared": 0}
