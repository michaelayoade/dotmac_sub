"""Service helpers for billing overview/invoice list/aging pages."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber
from app.services import billing as billing_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services.common import validate_enum

_BUCKET_SEQUENCE = ("current", "1_30", "31_60", "61_90", "90_plus")
_BUCKET_LABELS = {
    "current": "Current",
    "1_30": "1-30 Days",
    "31_60": "31-60 Days",
    "61_90": "61-90 Days",
    "90_plus": "90+ Days",
}


def _add_months(value: date, months: int) -> date:
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    return date(year, month, 1)


def _month_end(value: date) -> date:
    next_month = _add_months(value, 1)
    return next_month - timedelta(days=1)


def build_overview_data(
    db,
    *,
    partner_id: str | None = None,
    location: str | None = None,
) -> dict[str, object]:  # type: ignore[type-arg]
    """Build billing dashboard data via centralized reporting service."""
    from app.services.billing.reporting import billing_reporting

    result = billing_reporting.get_dashboard_stats(
        db,
        partner_id=partner_id,
        location=location,
    )
    result["selected_partner_id"] = (partner_id or "").strip() or None
    result["selected_location"] = (location or "").strip() or None

    invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=2000,
        offset=0,
    )
    partner_options: dict[str, str] = {}
    location_options: set[str] = set()
    for invoice in invoices:
        account = getattr(invoice, "account", None)
        if not account:
            continue
        reseller = getattr(account, "reseller", None)
        reseller_id = getattr(account, "reseller_id", None)
        if reseller_id:
            label = getattr(reseller, "name", None) or f"Partner {str(reseller_id)[:8]}"
            partner_options[str(reseller_id)] = str(label)
        region_value = (
            getattr(account, "region", None)
            or getattr(account, "billing_region", None)
            or getattr(account, "city", None)
        )
        if region_value:
            location_options.add(str(region_value))

    result["partner_options"] = [
        {"id": key, "name": value}
        for key, value in sorted(partner_options.items(), key=lambda item: item[1].lower())
    ]
    result["location_options"] = sorted(location_options)
    return result


def build_invoices_list_data(
    db,
    *,
    account_id: str | None,
    partner_id: str | None,
    status: str | None,
    proforma_only: bool = False,
    customer_ref: str | None,
    search: str | None,
    date_range: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    def _apply_filters(query, *, include_status: bool):  # type: ignore[no-untyped-def]
        scoped = query.filter(Invoice.is_active.is_(True))
        if account_ids:
            scoped = scoped.filter(Invoice.account_id.in_(account_ids))
        elif account_id:
            scoped = scoped.filter(Invoice.account_id == UUID(account_id))
        if selected_partner_id:
            scoped = scoped.filter(Invoice.account.has(Subscriber.reseller_id == selected_partner_id))

        if include_status and status:
            scoped = scoped.filter(Invoice.status == validate_enum(status, InvoiceStatus, "status"))
        if proforma_only:
            scoped = scoped.filter(
                or_(
                    Invoice.is_proforma.is_(True),
                    Invoice.memo.ilike("%[PROFORMA]%"),
                    Invoice.invoice_number.ilike("PF-%"),
                )
            )

        if search:
            term = f"%{search.strip()}%"
            scoped = scoped.filter(
                (Invoice.invoice_number.ilike(term)) | (Invoice.memo.ilike(term))
            )

        if date_range in {"today", "week", "month", "quarter"}:
            now = datetime.now(UTC)
            if date_range == "today":
                start = datetime(now.year, now.month, now.day, tzinfo=UTC)
            elif date_range == "week":
                start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
            elif date_range == "month":
                start = datetime(now.year, now.month, 1, tzinfo=UTC)
            else:
                quarter_start_month = ((now.month - 1) // 3) * 3 + 1
                start = datetime(now.year, quarter_start_month, 1, tzinfo=UTC)
            scoped = scoped.filter(Invoice.created_at >= start)
        return scoped

    def _build_status_totals(items: list[Invoice]) -> dict[str, dict[str, float | int]]:
        summary: dict[str, dict[str, float | int]] = {
            key: {"count": 0, "amount": 0.0}
            for key in ("draft", "issued", "partially_paid", "paid", "overdue", "void")
        }
        due_total = 0.0
        received_total = 0.0
        for invoice in items:
            raw_status = getattr(invoice, "status", InvoiceStatus.draft)
            status_key = raw_status.value if isinstance(raw_status, InvoiceStatus) else str(raw_status)
            if status_key not in summary:
                summary[status_key] = {"count": 0, "amount": 0.0}
            amount = float(getattr(invoice, "total", 0) or 0)
            due = float(getattr(invoice, "balance_due", 0) or 0)
            received = max(amount - due, 0.0)
            summary[status_key]["count"] = int(summary[status_key]["count"]) + 1
            summary[status_key]["amount"] = float(summary[status_key]["amount"]) + amount
            due_total += due
            received_total += received

        summary["all"] = {
            "count": len(items),
            "amount": sum(float(getattr(invoice, "total", 0) or 0) for invoice in items),
            "due_total": due_total,
            "received_total": received_total,
        }
        return summary

    offset = (page - 1) * per_page
    selected_partner_id = None
    if partner_id:
        try:
            selected_partner_id = UUID(partner_id)
        except ValueError:
            selected_partner_id = None
    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [UUID(item["id"]) for item in web_billing_customers_service.accounts_for_customer(db, customer_ref)]

    invoices: list[Invoice] = []
    total = 0
    filtered_for_summary: list[Invoice] = []
    if account_ids or not customer_filtered:
        filtered_query = _apply_filters(db.query(Invoice), include_status=True)
        invoices = (
            filtered_query
            .order_by(Invoice.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
        total = filtered_query.count()
        filtered_for_summary = (
            _apply_filters(db.query(Invoice), include_status=True)
            .order_by(Invoice.created_at.desc())
            .all()
        )

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    status_totals = _build_status_totals(filtered_for_summary)
    proforma_summary = web_billing_invoices_service.build_proforma_summary(filtered_for_summary)
    partner_options = [
        {"id": str(item.id), "name": item.name}
        for item in db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .order_by(Reseller.name.asc())
        .all()
    ]
    return {
        "invoices": invoices,
        "status_totals": status_totals,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "account_id": account_id,
        "selected_partner_id": str(selected_partner_id) if selected_partner_id else None,
        "partner_options": partner_options,
        "status": status,
        "proforma_only": proforma_only,
        "proforma_summary": proforma_summary,
        "customer_ref": customer_ref,
        "search": search,
        "date_range": date_range,
    }


def render_invoices_csv(invoices: list[Invoice]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "invoice_id",
            "invoice_number",
            "account_id",
            "status",
            "total",
            "balance_due",
            "payment_received",
            "currency",
            "issued_at",
            "due_at",
            "created_at",
            "memo",
        ]
    )
    for invoice in invoices:
        total = Decimal(str(getattr(invoice, "total", 0) or 0))
        due = Decimal(str(getattr(invoice, "balance_due", 0) or 0))
        received = total - due
        raw_status = getattr(invoice, "status", None)
        status_value = raw_status.value if hasattr(raw_status, "value") else str(raw_status or "")
        writer.writerow(
            [
                str(invoice.id),
                invoice.invoice_number or "",
                str(invoice.account_id) if invoice.account_id else "",
                status_value,
                f"{total:.2f}",
                f"{due:.2f}",
                f"{received:.2f}",
                invoice.currency or "NGN",
                invoice.issued_at.isoformat() if invoice.issued_at else "",
                invoice.due_at.isoformat() if invoice.due_at else "",
                invoice.created_at.isoformat() if invoice.created_at else "",
                invoice.memo or "",
            ]
        )
    return buffer.getvalue()


def _account_label(invoice: Invoice) -> str:
    account = getattr(invoice, "account", None)
    if not account:
        return "Account"

    display_name = getattr(account, "display_name", None)
    if display_name:
        return str(display_name)

    first_name = (getattr(account, "first_name", "") or "").strip()
    last_name = (getattr(account, "last_name", "") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part)
    if full_name:
        return full_name

    email = getattr(account, "email", None)
    if email:
        return str(email)

    account_number = getattr(account, "account_number", None)
    if account_number:
        return str(account_number)
    return "Account"


def _last_payment_date(invoice: Invoice) -> date | None:
    last_seen: datetime | None = None
    for allocation in getattr(invoice, "payment_allocations", []) or []:
        payment = getattr(allocation, "payment", None)
        if payment:
            candidate = getattr(payment, "paid_at", None) or getattr(allocation, "created_at", None)
        else:
            candidate = getattr(allocation, "created_at", None)
        if candidate and (last_seen is None or candidate > last_seen):
            last_seen = candidate
    return last_seen.date() if last_seen else None


def _classify_aging_bucket(invoice: Invoice, *, today: date) -> str:
    due_at = invoice.due_at.date() if invoice.due_at else None
    if not due_at or due_at >= today:
        return "current"

    days = (today - due_at).days
    if days <= 30:
        return "1_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


def _in_period(invoice: Invoice, *, period: str, today: date) -> bool:
    if period == "all":
        return True
    if period == "this_year":
        due_at = invoice.due_at.date() if invoice.due_at else None
        return bool(due_at and due_at.year == today.year)
    return False


def build_ar_aging_data(
    db,
    *,
    period: str = "all",
    bucket: str | None = None,
    partner_id: str | None = None,
    location: str | None = None,
    debtor_period: str | None = None,
) -> dict[str, object]:
    selected_period = period if period in {"all", "this_year"} else "all"
    selected_bucket = bucket if bucket in _BUCKET_SEQUENCE else None
    selected_partner_id = (partner_id or "").strip() or None
    selected_location = (location or "").strip() or None
    selected_debtor_period = debtor_period if debtor_period in {"all", "this_month", "last_month"} else "all"

    invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="due_at",
        order_dir="asc",
        limit=2000,
        offset=0,
    )

    today = datetime.now(UTC).date()
    period_filtered_invoices = [
        invoice
        for invoice in invoices
        if invoice.status not in {InvoiceStatus.paid, InvoiceStatus.void}
        and _in_period(invoice, period=selected_period, today=today)
    ]

    partner_options: dict[str, str] = {}
    location_options: set[str] = set()
    for invoice in period_filtered_invoices:
        account = getattr(invoice, "account", None)
        if account is None:
            continue
        reseller = getattr(account, "reseller", None)
        reseller_id = getattr(account, "reseller_id", None)
        if reseller_id:
            label = getattr(reseller, "name", None) or f"Partner {str(reseller_id)[:8]}"
            partner_options[str(reseller_id)] = str(label)
        region_value = (
            getattr(account, "region", None)
            or getattr(account, "billing_region", None)
            or getattr(account, "city", None)
        )
        if region_value:
            location_options.add(str(region_value))

    filtered_invoices = []
    for invoice in period_filtered_invoices:
        account = getattr(invoice, "account", None)
        if selected_partner_id:
            account_partner = str(getattr(account, "reseller_id", "") or "")
            if account_partner != selected_partner_id:
                continue
        if selected_location:
            account_location = (
                str(getattr(account, "region", "") or "")
                or str(getattr(account, "billing_region", "") or "")
                or str(getattr(account, "city", "") or "")
            )
            if account_location.lower() != selected_location.lower():
                continue
        filtered_invoices.append(invoice)

    buckets: dict[str, list[Invoice]] = {key: [] for key in _BUCKET_SEQUENCE}
    for invoice in filtered_invoices:
        bucket_key = _classify_aging_bucket(invoice, today=today)
        buckets[bucket_key].append(invoice)

    totals = {
        key: sum(float(getattr(inv, "balance_due", 0) or 0) for inv in items)
        for key, items in buckets.items()
    }
    counts = {key: len(items) for key, items in buckets.items()}

    bucket_rows: dict[str, list[dict[str, object]]] = {}
    bucket_invoice_ids: dict[str, str] = {}
    for key, items in buckets.items():
        bucket_rows[key] = [
            {
                "invoice": invoice,
                "account_label": _account_label(invoice),
                "last_payment_at": _last_payment_date(invoice),
            }
            for invoice in items
        ]
        bucket_invoice_ids[key] = ",".join(str(invoice.id) for invoice in items)

    overdue_invoices = [
        invoice
        for key in ("1_30", "31_60", "61_90", "90_plus")
        for invoice in buckets[key]
    ]
    this_month_start = date(today.year, today.month, 1)
    previous_month_end = this_month_start - timedelta(days=1)
    last_month_start = date(previous_month_end.year, previous_month_end.month, 1)

    def _debtor_period_match(invoice: Invoice) -> bool:
        if selected_debtor_period == "all":
            return True
        due_at = invoice.due_at.date() if invoice.due_at else None
        if not due_at:
            return False
        if selected_debtor_period == "this_month":
            return this_month_start <= due_at <= today
        if selected_debtor_period == "last_month":
            return last_month_start <= due_at <= previous_month_end
        return True

    debtor_totals: dict[UUID, float] = {}
    debtor_names: dict[UUID, str] = {}
    for invoice in overdue_invoices:
        if not _debtor_period_match(invoice):
            continue
        account_id = invoice.account_id
        debtor_totals[account_id] = debtor_totals.get(account_id, 0.0) + float(invoice.balance_due or 0)
        debtor_names.setdefault(account_id, _account_label(invoice))

    top_debtors = [
        {
            "account_id": str(account_id),
            "account_label": debtor_names.get(account_id, "Account"),
            "amount": amount,
        }
        for account_id, amount in sorted(debtor_totals.items(), key=lambda item: item[1], reverse=True)[:10]
    ]

    bucket_order = [
        {
            "key": key,
            "label": _BUCKET_LABELS[key],
            "amount": totals[key],
            "count": counts[key],
            "is_selected": selected_bucket == key,
        }
        for key in _BUCKET_SEQUENCE
    ]

    visible_keys = [selected_bucket] if selected_bucket else list(_BUCKET_SEQUENCE)

    # Aging trend over last 6 months (snapshot at month-end).
    current_month = date(today.year, today.month, 1)
    trend_months = [_add_months(current_month, -5 + idx) for idx in range(6)]
    trend_labels = [month.strftime("%b %Y") for month in trend_months]
    trend_series: dict[str, list[float]] = {key: [] for key in _BUCKET_SEQUENCE}
    for month_start in trend_months:
        snapshot_at = _month_end(month_start)
        snapshot_buckets: dict[str, float] = {key: 0.0 for key in _BUCKET_SEQUENCE}
        for invoice in filtered_invoices:
            due_at = invoice.due_at.date() if invoice.due_at else None
            if not due_at:
                snapshot_buckets["current"] += float(invoice.balance_due or 0)
                continue
            if due_at >= snapshot_at:
                snapshot_buckets["current"] += float(invoice.balance_due or 0)
                continue
            days = (snapshot_at - due_at).days
            if days <= 30:
                snapshot_buckets["1_30"] += float(invoice.balance_due or 0)
            elif days <= 60:
                snapshot_buckets["31_60"] += float(invoice.balance_due or 0)
            elif days <= 90:
                snapshot_buckets["61_90"] += float(invoice.balance_due or 0)
            else:
                snapshot_buckets["90_plus"] += float(invoice.balance_due or 0)
        for key in _BUCKET_SEQUENCE:
            trend_series[key].append(snapshot_buckets[key])

    return {
        "buckets": buckets,
        "totals": totals,
        "counts": counts,
        "bucket_rows": bucket_rows,
        "bucket_invoice_ids": bucket_invoice_ids,
        "bucket_order": bucket_order,
        "visible_bucket_keys": visible_keys,
        "selected_bucket": selected_bucket,
        "selected_period": selected_period,
        "selected_partner_id": selected_partner_id,
        "selected_location": selected_location,
        "selected_debtor_period": selected_debtor_period,
        "partner_options": [
            {"id": partner_key, "name": partner_name}
            for partner_key, partner_name in sorted(partner_options.items(), key=lambda item: item[1].lower())
        ],
        "location_options": sorted(location_options),
        "top_debtors": top_debtors,
        "aging_trend": {
            "labels": trend_labels,
            "series": trend_series,
        },
    }
