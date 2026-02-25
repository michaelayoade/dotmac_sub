"""Service helpers for billing bank reconciliation workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import BankReconciliationItem, BankReconciliationRun, Payment
from app.services import web_billing_payments as web_billing_payments_service


def _date_start_for_range(date_range: str | None) -> datetime | None:
    now = datetime.now(UTC)
    if date_range == "today":
        return datetime(now.year, now.month, now.day, tzinfo=UTC)
    if date_range == "week":
        return datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
    if date_range == "month":
        return datetime(now.year, now.month, 1, tzinfo=UTC)
    if date_range == "quarter":
        quarter_start_month = ((now.month - 1) // 3) * 3 + 1
        return datetime(now.year, quarter_start_month, 1, tzinfo=UTC)
    return None


def build_reconciliation_data(
    db,
    *,
    date_range: str | None,
    handler: str | None,
) -> dict[str, object]:
    history_rows = web_billing_payments_service.list_payment_import_history_filtered(
        db,
        limit=200,
        handler=handler,
        status=None,
        date_range=date_range,
    )
    start = _date_start_for_range(date_range)
    payment_query = db.query(Payment).filter(Payment.is_active.is_(True))
    if start is not None:
        payment_query = payment_query.filter(Payment.created_at >= start)
    payments = payment_query.order_by(Payment.created_at.desc()).limit(500).all()

    statement_total = sum(Decimal(str(item.get("total_amount", 0) or 0)) for item in history_rows)
    statement_rows = sum(int(item.get("row_count", 0) or 0) for item in history_rows)
    imported_rows = sum(int(item.get("matched_count", 0) or 0) for item in history_rows)
    unmatched_rows = sum(int(item.get("unmatched_count", 0) or 0) for item in history_rows)
    payment_total = sum(Decimal(str(getattr(item, "amount", 0) or 0)) for item in payments)

    by_external_id: dict[str, list[Payment]] = {}
    for payment in payments:
        external_id = str(getattr(payment, "external_id", "") or "").strip()
        if not external_id:
            continue
        by_external_id.setdefault(external_id, []).append(payment)
    duplicate_candidates = [
        {
            "external_id": key,
            "count": len(group),
            "total_amount": float(sum(Decimal(str(getattr(item, "amount", 0) or 0)) for item in group)),
        }
        for key, group in by_external_id.items()
        if len(group) > 1
    ]
    duplicate_candidates.sort(key=lambda item: item["count"], reverse=True)

    unmatched_imports = [item for item in history_rows if int(item.get("unmatched_count", 0) or 0) > 0][:25]
    partial_imports = [item for item in history_rows if str(item.get("status")) == "partial"][:25]

    run = BankReconciliationRun(
        date_range=(date_range or "").strip() or None,
        handler=(handler or "").strip() or None,
        statement_rows=statement_rows,
        imported_rows=imported_rows,
        unmatched_rows=unmatched_rows,
        system_payment_count=len(payments),
        statement_total=statement_total,
        payment_total=payment_total,
        difference_total=(statement_total - payment_total),
    )
    db.add(run)
    db.flush()

    items: list[BankReconciliationItem] = []
    for row in unmatched_imports:
        items.append(
            BankReconciliationItem(
                run_id=run.id,
                item_type="unmatched",
                reference=str(row.get("handler") or ""),
                file_name=str(row.get("file_name") or ""),
                count=int(row.get("unmatched_count", 0) or 0),
                amount=Decimal(str(row.get("total_amount", 0) or 0)),
                metadata_={"status": row.get("status"), "occurred_at": str(row.get("occurred_at") or "")},
            )
        )
    for row in duplicate_candidates[:25]:
        items.append(
            BankReconciliationItem(
                run_id=run.id,
                item_type="duplicate",
                reference=str(row.get("external_id") or ""),
                file_name=None,
                count=int(row.get("count", 0) or 0),
                amount=Decimal(str(row.get("total_amount", 0) or 0)),
                metadata_={},
            )
        )
    if items:
        db.add_all(items)
    db.commit()

    return {
        "last_run_id": str(run.id),
        "date_range": date_range or "",
        "selected_handler": (handler or "").strip() or "",
        "handler_options": [
            {"id": "base_csv", "name": "Base (CSV)"},
            {"id": "zenith_bank", "name": "Zenith Bank"},
            {"id": "gtbank", "name": "GTBank"},
            {"id": "access_bank", "name": "Access Bank"},
            {"id": "fixed_width_basic", "name": "Fixed Width (Basic)"},
        ],
        "summary": {
            "statement_rows": statement_rows,
            "imported_rows": imported_rows,
            "unmatched_rows": unmatched_rows,
            "system_payment_count": len(payments),
            "statement_total": float(statement_total),
            "payment_total": float(payment_total),
            "difference": float(statement_total - payment_total),
        },
        "unmatched_imports": unmatched_imports,
        "partial_imports": partial_imports,
        "duplicate_candidates": duplicate_candidates[:25],
    }
