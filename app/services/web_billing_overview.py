"""Service helpers for billing overview/invoice list/aging pages."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import SubscriberStatus
from app.services import billing as billing_service
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services.common import validate_enum


def build_overview_data(db) -> dict[str, object]:
    invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    all_invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    stats = {
        "total_revenue": sum(float(getattr(inv, "total", 0) or 0) for inv in all_invoices if inv.status == InvoiceStatus.paid),
        "pending_amount": sum(
            float(getattr(inv, "total", 0) or 0) for inv in all_invoices if inv.status == InvoiceStatus.issued
        ),
        "overdue_amount": sum(float(getattr(inv, "total", 0) or 0) for inv in all_invoices if inv.status == InvoiceStatus.overdue),
        "total_invoices": len(all_invoices),
        "paid_count": sum(1 for inv in all_invoices if inv.status == InvoiceStatus.paid),
        "pending_count": sum(1 for inv in all_invoices if inv.status == InvoiceStatus.issued),
        "overdue_count": sum(1 for inv in all_invoices if inv.status == InvoiceStatus.overdue),
        "draft_count": sum(1 for inv in all_invoices if inv.status == InvoiceStatus.draft),
    }

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=2000,
        offset=0,
    )
    return {
        "invoices": invoices,
        "stats": stats,
        "total_balance": sum((getattr(account, "balance", 0) or 0) for account in accounts),
        "active_count": sum(
            1 for account in accounts if account.status == SubscriberStatus.active
        ),
        "suspended_count": sum(
            1 for account in accounts if account.status == SubscriberStatus.suspended
        ),
    }


def build_invoices_list_data(
    db,
    *,
    account_id: str | None,
    status: str | None,
    customer_ref: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [UUID(item["id"]) for item in web_billing_customers_service.accounts_for_customer(db, customer_ref)]

    invoices = []
    if account_ids:
        query = db.query(Invoice).filter(Invoice.account_id.in_(account_ids)).filter(Invoice.is_active.is_(True))
        if status:
            query = query.filter(Invoice.status == validate_enum(status, InvoiceStatus, "status"))
        invoices = query.order_by(Invoice.created_at.desc()).offset(offset).limit(per_page).all()
    elif not customer_filtered:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=account_id if account_id else None,
            status=status if status else None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )

    if account_ids:
        count_query = db.query(Invoice).filter(Invoice.account_id.in_(account_ids)).filter(Invoice.is_active.is_(True))
        if status:
            count_query = count_query.filter(Invoice.status == validate_enum(status, InvoiceStatus, "status"))
        total = count_query.count()
    elif not customer_filtered:
        total_query = db.query(Invoice).filter(Invoice.is_active.is_(True))
        if account_id:
            total_query = total_query.filter(Invoice.account_id == UUID(account_id))
        if status:
            total_query = total_query.filter(Invoice.status == validate_enum(status, InvoiceStatus, "status"))
        total = total_query.count()
    else:
        total = 0

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    return {
        "invoices": invoices,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "account_id": account_id,
        "status": status,
        "customer_ref": customer_ref,
    }


def build_ar_aging_data(db) -> dict[str, object]:
    invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="due_at",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    today = datetime.now(UTC).date()
    buckets: dict[str, list[Invoice]] = {
        "current": [],
        "1_30": [],
        "31_60": [],
        "61_90": [],
        "90_plus": [],
    }
    for invoice in invoices:
        if invoice.status in {InvoiceStatus.paid, InvoiceStatus.void}:
            continue
        due_at = invoice.due_at.date() if invoice.due_at else None
        if not due_at or due_at >= today:
            buckets["current"].append(invoice)
            continue
        days = (today - due_at).days
        if days <= 30:
            buckets["1_30"].append(invoice)
        elif days <= 60:
            buckets["31_60"].append(invoice)
        elif days <= 90:
            buckets["61_90"].append(invoice)
        else:
            buckets["90_plus"].append(invoice)
    totals = {
        key: sum(float(getattr(inv, "balance_due", 0) or 0) for inv in items)
        for key, items in buckets.items()
    }
    return {"buckets": buckets, "totals": totals}
