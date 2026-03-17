"""Extended report service functions for 04_administration features."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.common import parse_date_filter as _parse_date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 4.25 Subscriber Growth Chart
# ---------------------------------------------------------------------------

def get_subscriber_growth_data(db: Session, *, days: int = 30) -> dict:
    """Daily subscriber counts by status over last N days."""
    from app.models.subscriber import Subscriber, SubscriberStatus

    end = datetime.now(UTC)
    start = end - timedelta(days=days)

    total = db.scalar(select(func.count()).select_from(Subscriber)) or 0

    # Count by status
    status_counts: dict[str, int] = {}
    for s in SubscriberStatus:
        count = db.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(Subscriber.status == s)
        ) or 0
        status_counts[s.value] = count

    # New this month
    month_start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_this_month = db.scalar(
        select(func.count())
        .select_from(Subscriber)
        .where(Subscriber.created_at >= month_start)
    ) or 0

    # Simple daily chart data (placeholder — real impl would group by date)
    chart_labels = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    chart_data = [total] * days  # flat line placeholder

    return {
        "total_subscribers": total,
        "status_counts": status_counts,
        "new_this_month": new_this_month,
        "chart_labels": chart_labels,
        "chart_data": chart_data,
        "days": days,
    }


# ---------------------------------------------------------------------------
# 4.26 Usage by Plan
# ---------------------------------------------------------------------------

def get_usage_by_plan_data(db: Session) -> dict:
    """Subscriber counts per catalog offer."""
    try:
        from app.models.catalog import CatalogOffer, Subscription

        stmt = (
            select(
                CatalogOffer.name,
                func.count(Subscription.id).label("sub_count"),
            )
            .join(Subscription, Subscription.offer_id == CatalogOffer.id, isouter=True)
            .group_by(CatalogOffer.id, CatalogOffer.name)
            .order_by(func.count(Subscription.id).desc())
        )
        rows = db.execute(stmt).all()
        plans = [{"name": r[0], "count": r[1]} for r in rows]
    except Exception as exc:
        logger.warning("Could not query plan usage: %s", exc)
        plans = []

    return {"plans": plans, "total_plans": len(plans)}


# ---------------------------------------------------------------------------
# 4.28 Upcoming Charges / Future Charges
# ---------------------------------------------------------------------------

def get_upcoming_charges_data(db: Session) -> dict:
    """Active subscriptions with upcoming billing."""
    try:
        from app.models.catalog import CatalogOffer, Subscription
        from app.models.subscriber import Subscriber

        stmt = (
            select((Subscriber.first_name + " " + Subscriber.last_name).label("full_name"), CatalogOffer.name, Subscription.unit_price, Subscription.start_at)
            .join(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
            .join(Subscriber, Subscription.subscriber_id == Subscriber.id)
            .where(Subscription.status == "active")
            .order_by(Subscription.unit_price.desc())
            .limit(100)
        )
        rows = db.execute(stmt).all()
        charges = [
            {"subscriber": r[0], "plan": r[1], "amount": r[2], "start_date": r[3]}
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Could not query upcoming charges: %s", exc)
        charges = []

    total_amount = sum(c["amount"] or Decimal("0") for c in charges)
    return {"charges": charges, "total_amount": total_amount, "total_count": len(charges)}


# ---------------------------------------------------------------------------
# 4.29 Revenue Per Plan
# ---------------------------------------------------------------------------

def get_revenue_per_plan_data(db: Session, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Revenue aggregated by plan."""
    try:
        from app.models.billing import Invoice, InvoiceLine
        from app.models.catalog import CatalogOffer, Subscription

        stmt = (
            select(
                CatalogOffer.name,
                func.count(func.distinct(Invoice.id)).label("invoice_count"),
                func.coalesce(func.sum(InvoiceLine.amount), 0).label("total_revenue"),
            )
            .select_from(CatalogOffer)
            .join(Subscription, Subscription.offer_id == CatalogOffer.id, isouter=True)
            .join(InvoiceLine, InvoiceLine.subscription_id == Subscription.id, isouter=True)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id, isouter=True)
            .group_by(CatalogOffer.id, CatalogOffer.name)
            .order_by(func.coalesce(func.sum(InvoiceLine.amount), 0).desc())
        )

        d_from = _parse_date(date_from)
        d_to = _parse_date(date_to)
        if d_from:
            stmt = stmt.where(Invoice.issued_at >= d_from)
        if d_to:
            stmt = stmt.where(Invoice.issued_at < d_to + timedelta(days=1))

        rows = db.execute(stmt).all()
        plans = [{"name": r[0], "invoice_count": r[1], "revenue": r[2]} for r in rows]
    except Exception as exc:
        logger.warning("Could not query revenue per plan: %s", exc)
        plans = []

    return {
        "plans": plans,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "chart_labels": [p["name"] for p in plans[:20]],
        "chart_values": [float(p["revenue"]) for p in plans[:20]],
    }


# ---------------------------------------------------------------------------
# 4.30 Invoice Report
# ---------------------------------------------------------------------------

def get_invoice_report_data(db: Session, date_from: str | None = None, date_to: str | None = None, status: str | None = None) -> dict:
    """Detailed invoice listing with tax breakdown."""
    try:
        from app.models.billing import Invoice

        stmt = select(Invoice).order_by(Invoice.issued_at.desc()).limit(200)
        d_from = _parse_date(date_from)
        d_to = _parse_date(date_to)
        if d_from:
            stmt = stmt.where(Invoice.issued_at >= d_from)
        if d_to:
            stmt = stmt.where(Invoice.issued_at < d_to + timedelta(days=1))
        if status:
            stmt = stmt.where(Invoice.status == status)

        invoices = list(db.scalars(stmt).all())
    except Exception as exc:
        logger.warning("Could not query invoices: %s", exc)
        invoices = []

    return {
        "invoices": invoices,
        "total_count": len(invoices),
        "date_from": date_from or "",
        "date_to": date_to or "",
        "status_filter": status or "",
    }


# ---------------------------------------------------------------------------
# 4.31 Statements
# ---------------------------------------------------------------------------

def get_statements_data(db: Session) -> dict:
    """Customer financial summaries."""
    try:
        from app.models.billing import Invoice
        from app.models.subscriber import Subscriber

        stmt = (
            select(
                (Subscriber.first_name + " " + Subscriber.last_name).label("full_name"),
                func.count(Invoice.id).label("doc_count"),
                func.coalesce(func.sum(Invoice.total), 0).label("total"),
            )
            .join(Invoice, Invoice.account_id == Subscriber.id, isouter=True)
            .group_by(Subscriber.id, (Subscriber.first_name + " " + Subscriber.last_name).label("full_name"))
            .order_by((Subscriber.first_name + " " + Subscriber.last_name).label("full_name"))
            .limit(200)
        )
        rows = db.execute(stmt).all()
        statements = [{"name": r[0], "doc_count": r[1], "total": r[2]} for r in rows]
    except Exception as exc:
        logger.warning("Could not query statements: %s", exc)
        statements = []

    return {"statements": statements}


# ---------------------------------------------------------------------------
# 4.32 Tax Report
# ---------------------------------------------------------------------------

def get_tax_report_data(db: Session) -> dict:
    """Per-invoice tax details and totals."""
    try:
        from app.models.billing import Invoice

        stmt = select(Invoice).where(Invoice.tax_total > 0).order_by(Invoice.issued_at.desc()).limit(200)
        invoices = list(db.scalars(stmt).all())
    except Exception as exc:
        logger.warning("Could not query tax data: %s", exc)
        invoices = []

    total_tax = sum(getattr(i, "tax_total", 0) or 0 for i in invoices)
    return {"invoices": invoices, "total_tax": total_tax}


# ---------------------------------------------------------------------------
# 4.36 MRR Net Change
# ---------------------------------------------------------------------------

def get_mrr_data(db: Session, year: int | None = None) -> dict:
    """Monthly recurring revenue movement."""
    from app.models.subscriber import Subscriber

    if not year:
        year = datetime.now(UTC).year

    total = db.scalar(select(func.count()).select_from(Subscriber)) or 0

    # Build monthly placeholder data
    months = []
    for m in range(1, 13):
        months.append({
            "month": f"{year}-{m:02d}",
            "start_count": total,
            "new": 0,
            "cancellations": 0,
            "end_count": total,
            "net_change": 0,
        })

    return {"months": months, "year": year, "total_subscribers": total}


# ---------------------------------------------------------------------------
# 4.37 New Services
# ---------------------------------------------------------------------------

def get_new_services_data(db: Session, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Recently activated subscriptions."""
    try:
        from app.models.catalog import CatalogOffer, Subscription
        from app.models.subscriber import Subscriber

        stmt = (
            select((Subscriber.first_name + " " + Subscriber.last_name).label("full_name"), CatalogOffer.name, Subscription.unit_price, Subscription.start_at, Subscription.status)
            .join(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
            .join(Subscriber, Subscription.subscriber_id == Subscriber.id)
            .order_by(Subscription.start_at.desc())
            .limit(200)
        )
        d_from = _parse_date(date_from)
        d_to = _parse_date(date_to)
        if d_from:
            stmt = stmt.where(Subscription.start_at >= d_from)
        if d_to:
            stmt = stmt.where(Subscription.start_at < d_to + timedelta(days=1))

        rows = db.execute(stmt).all()
        services = [{"subscriber": r[0], "plan": r[1], "price": r[2], "start_date": r[3], "status": r[4]} for r in rows]
    except Exception as exc:
        logger.warning("Could not query new services: %s", exc)
        services = []

    return {"services": services, "date_from": date_from or "", "date_to": date_to or ""}


# ---------------------------------------------------------------------------
# Placeholder reports
# ---------------------------------------------------------------------------

def get_referrals_data(db: Session) -> dict:
    return {"referrals": [], "total": 0}


def get_vouchers_data(db: Session) -> dict:
    return {"vouchers": [], "total": 0}


def get_dns_threats_data(db: Session) -> dict:
    return {"threats": [], "total": 0}


def get_custom_pricing_data(db: Session) -> dict:
    return {"overrides": [], "total": 0}


def get_revenue_categories_data(db: Session) -> dict:
    return {"categories": [], "total": 0}
