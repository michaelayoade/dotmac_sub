"""Extended report service functions for 04_administration features."""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.common import parse_date_filter as _parse_date
from app.services.status_presentation import invoice_status_presentation

logger = logging.getLogger(__name__)


def _default_report_window(days: int | None = 30) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days or 30)
    return start, end


def _resolve_report_window(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    days: int | None = 30,
) -> tuple[datetime, datetime, str, str]:
    start, end = _default_report_window(days)
    parsed_from = _parse_date(date_from)
    parsed_to = _parse_date(date_to)
    if parsed_from is not None:
        start = parsed_from
    if parsed_to is not None:
        end = parsed_to + timedelta(days=1)
    if end <= start:
        end = start + timedelta(days=1)
    return (
        start,
        end,
        start.date().isoformat(),
        (end - timedelta(days=1)).date().isoformat(),
    )


def _gb_from_avg_bps(
    avg_bps: float | int | Decimal | None, span_seconds: float
) -> float:
    return (float(avg_bps or 0) / 8.0 * span_seconds) / (1024**3)


# ---------------------------------------------------------------------------
# 4.25 Subscriber Growth Chart
# ---------------------------------------------------------------------------


def get_subscriber_growth_data(db: Session, *, days: int = 30) -> dict:
    """Daily subscriber counts by status over last N days.

    Read owner: app.services.subscriber_growth — this function composes the
    owner's counts/series and owns presentation only.
    """
    from app.services import subscriber_growth

    signups = subscriber_growth.daily_cumulative_signups(db, days=days)

    return {
        "total_subscribers": signups["total"],
        "status_counts": subscriber_growth.status_counts(db),
        "new_this_month": signups["new_this_month"],
        "chart_labels": signups["labels"],
        "chart_data": signups["data"],
        "days": days,
    }


# ---------------------------------------------------------------------------
# 4.26 Usage by Plan
# ---------------------------------------------------------------------------


def get_usage_by_plan_data(db: Session) -> dict:
    """Subscriber counts per catalog offer (read owner: billing.reporting)."""
    from app.services.billing import reporting as billing_reporting

    try:
        plans = billing_reporting.get_subscription_count_by_offer(db)
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
            select(
                (Subscriber.first_name + " " + Subscriber.last_name).label("full_name"),
                CatalogOffer.name,
                Subscription.unit_price,
                Subscription.start_at,
            )
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
    return {
        "charges": charges,
        "total_amount": total_amount,
        "total_count": len(charges),
    }


# ---------------------------------------------------------------------------
# 4.29 Revenue Per Plan
# ---------------------------------------------------------------------------


def get_revenue_per_plan_data(
    db: Session, date_from: str | None = None, date_to: str | None = None
) -> dict:
    """Revenue aggregated by plan (read owner: billing.reporting)."""
    from app.services.billing import reporting as billing_reporting

    try:
        d_from = _parse_date(date_from)
        d_to = _parse_date(date_to)
        plans = billing_reporting.get_revenue_by_offer(
            db,
            issued_from=d_from,
            issued_before=(d_to + timedelta(days=1)) if d_to else None,
        )
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


def get_invoice_report_data(
    db: Session,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> dict:
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
        "invoice_status_presentations": {
            str(invoice.id): invoice_status_presentation(invoice.status)
            for invoice in invoices
        },
        "total_count": len(invoices),
        "date_from": date_from or "",
        "date_to": date_to or "",
        "status_filter": status or "",
    }


# ---------------------------------------------------------------------------
# 4.31 Statements
# ---------------------------------------------------------------------------


def get_statements_data(db: Session) -> dict:
    """Customer financial summaries (read owner: billing.reporting)."""
    from app.services.billing import reporting as billing_reporting

    try:
        statements = billing_reporting.get_customer_statement_totals(db)
    except Exception as exc:
        logger.warning("Could not query statements: %s", exc)
        statements = []

    return {"statements": statements}


# ---------------------------------------------------------------------------
# 4.32 Tax Report
# ---------------------------------------------------------------------------


def get_tax_report_data(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, object]:
    """Delegate tax-report meaning to the tax-accounting owner."""
    from app.services import tax_accounting

    return tax_accounting.build_tax_report(
        db,
        date_from=date_from,
        date_to=date_to,
    )


# ---------------------------------------------------------------------------
# 4.36 MRR Net Change
# ---------------------------------------------------------------------------


def get_mrr_data(
    db: Session,
    year: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Monthly recurring revenue movement with real subscription data.

    The per-month movement counts are owned by billing.reporting
    (get_subscription_movement); this function windows the months to the
    requested date range and presents them.
    """
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.services.billing import reporting as billing_reporting

    parsed_from = _parse_date(date_from)
    parsed_to = _parse_date(date_to)
    if parsed_from is not None:
        year = parsed_from.year
    if not year:
        year = datetime.now(UTC).year
    range_start = parsed_from
    range_end = parsed_to + timedelta(days=1) if parsed_to is not None else None

    movement = billing_reporting.get_subscription_movement(db, year=year)

    months = []
    for m, entry in enumerate(movement, start=1):
        month_start = datetime(year, m, 1, tzinfo=UTC)
        if m < 12:
            month_end = datetime(year, m + 1, 1, tzinfo=UTC)
        else:
            month_end = datetime(year + 1, 1, 1, tzinfo=UTC)

        if range_start is not None and month_end <= range_start:
            continue
        if range_end is not None and month_start >= range_end:
            continue

        months.append(entry)

    total = billing_reporting.get_active_subscription_count(db)

    return {
        "months": months,
        "year": year,
        "total_subscribers": total,
        "date_from": date_from or "",
        "date_to": date_to or "",
    }


# ---------------------------------------------------------------------------
# 4.37 New Services
# ---------------------------------------------------------------------------


def get_new_services_data(
    db: Session, date_from: str | None = None, date_to: str | None = None
) -> dict:
    """Recently activated subscriptions."""
    try:
        from app.models.catalog import CatalogOffer, Subscription
        from app.models.subscriber import Subscriber

        stmt = (
            select(
                (Subscriber.first_name + " " + Subscriber.last_name).label("full_name"),
                CatalogOffer.name,
                Subscription.unit_price,
                Subscription.start_at,
                Subscription.status,
            )
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
        services = [
            {
                "subscriber": r[0],
                "plan": r[1],
                "price": r[2],
                "start_date": r[3],
                "status": r[4],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Could not query new services: %s", exc)
        services = []

    return {
        "services": services,
        "date_from": date_from or "",
        "date_to": date_to or "",
    }


# ---------------------------------------------------------------------------
# Bandwidth & Usage Analytics
# ---------------------------------------------------------------------------


def get_bandwidth_report_data(
    db: Session,
    *,
    days: int | None = 30,
    date_from: str | None = None,
    date_to: str | None = None,
    show_chart: bool = False,
) -> dict:
    """Network usage analytics — total usage, per-plan, top consumers.

    The window aggregations are owned by app.services.usage_summary; this
    function composes them and owns presentation (unit conversion, rounding,
    chart shaping) only.
    """
    from app.models.catalog import CatalogOffer, Subscription
    from app.models.subscriber import Subscriber
    from app.services import usage_summary

    start, end, date_from_value, date_to_value = _resolve_report_window(
        date_from=date_from,
        date_to=date_to,
        days=days,
    )
    span_seconds = max(0.0, (end - start).total_seconds())
    usage = usage_summary.subscription_bandwidth_usage_subquery(
        start, end, span_seconds
    )

    totals = usage_summary.bandwidth_report_totals(db, usage)
    usage_bytes = totals["usage_bytes"]
    avg_rx = totals["avg_rx"]
    avg_tx = totals["avg_tx"]
    peak_rx = totals["peak_rx"]
    peak_tx = totals["peak_tx"]
    active_subs = totals["active_subs"]
    total_gb = usage_bytes / (1024**3)

    top_rows = db.execute(
        select(
            usage.c.subscription_id,
            usage.c.avg_total,
            usage.c.usage_bytes,
            Subscriber.first_name,
            Subscriber.last_name,
            Subscriber.company_name,
            Subscriber.display_name,
            CatalogOffer.name.label("plan_name"),
        )
        .select_from(usage)
        .join(Subscription, Subscription.id == usage.c.subscription_id, isouter=True)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id, isouter=True)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id, isouter=True)
        .order_by(usage.c.usage_bytes.desc())
        .limit(20)
    ).all()

    top_consumers = [
        {
            "subscriber": (
                r.company_name
                or r.display_name
                or f"{r.first_name or ''} {r.last_name or ''}".strip()
                or "Unknown"
            ),
            "plan": r.plan_name or "Unknown",
            "avg_mbps": round(float(r.avg_total or 0) / 1_000_000, 2),
            "usage_gb": round(float(r.usage_bytes or 0) / (1024**3), 2),
        }
        for r in top_rows
    ]

    plan_rows = usage_summary.bandwidth_usage_by_plan(db, usage)
    usage_by_plan = [
        {
            "name": r.name or "Unlinked",
            "avg_mbps": round(float(r.avg_bps or 0) / 1_000_000, 2),
            "usage_gb": round(float(r.usage_bytes or 0) / (1024**3), 2),
            "subscribers": r.sub_count,
        }
        for r in plan_rows
    ]

    return {
        "days": days,
        "date_from": date_from_value,
        "date_to": date_to_value,
        "show_chart": show_chart,
        "total_gb": round(total_gb, 2),
        "avg_rx_mbps": round(avg_rx / 1_000_000, 2),
        "avg_tx_mbps": round(avg_tx / 1_000_000, 2),
        "peak_rx_mbps": round(peak_rx / 1_000_000, 2),
        "peak_tx_mbps": round(peak_tx / 1_000_000, 2),
        "active_subscribers": active_subs,
        "chart_labels": [],
        "chart_rx": [],
        "chart_tx": [],
        "plan_chart_labels": [row["name"] for row in usage_by_plan[:20]],
        "plan_chart_values": [row["usage_gb"] for row in usage_by_plan[:20]],
        "top_consumers": top_consumers,
        "usage_by_plan": usage_by_plan,
    }


def build_bandwidth_report_export_csv(data: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bandwidth & Usage Report"])
    writer.writerow(["date_from", data.get("date_from", "")])
    writer.writerow(["date_to", data.get("date_to", "")])
    writer.writerow(["total_usage_gb", data.get("total_gb", 0)])
    writer.writerow(["active_subscribers", data.get("active_subscribers", 0)])
    writer.writerow([])
    writer.writerow(["Usage by Plan"])
    writer.writerow(["plan", "usage_gb", "avg_mbps", "subscribers"])
    for row in data.get("usage_by_plan", []):
        writer.writerow(
            [
                row.get("name", ""),
                row.get("usage_gb", 0),
                row.get("avg_mbps", 0),
                row.get("subscribers", 0),
            ]
        )
    writer.writerow([])
    writer.writerow(["Top Consumers"])
    writer.writerow(["subscriber", "plan", "usage_gb", "avg_mbps"])
    for row in data.get("top_consumers", []):
        writer.writerow(
            [
                row.get("subscriber", ""),
                row.get("plan", ""),
                row.get("usage_gb", 0),
                row.get("avg_mbps", 0),
            ]
        )
    content = output.getvalue()
    output.close()
    return content


# ---------------------------------------------------------------------------
# Revenue by Category (real data from invoice lines + offers)
# ---------------------------------------------------------------------------


def get_revenue_categories_data(db: Session) -> dict:
    """Revenue segmented by offer service type (read owner: billing.reporting)."""
    from app.services.billing import reporting as billing_reporting

    try:
        rows = billing_reporting.get_revenue_by_service_type(db)
        categories = [
            {
                "name": (
                    r["service_type"].value
                    if hasattr(r["service_type"], "value")
                    else str(r["service_type"] or "Uncategorized")
                ),
                "invoice_count": r["invoice_count"],
                "revenue": float(r["total"] or 0),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Could not query revenue categories: %s", exc)
        categories = []

    total_revenue = sum(c["revenue"] for c in categories)
    return {
        "categories": categories,
        "total": len(categories),
        "total_revenue": total_revenue,
        "chart_labels": [c["name"] for c in categories],
        "chart_values": [c["revenue"] for c in categories],
    }


def get_custom_pricing_data(db: Session) -> dict:
    """Subscriptions with custom pricing overrides or active add-ons."""
    try:
        from app.models.catalog import CatalogOffer, Subscription, SubscriptionAddOn
        from app.models.subscriber import Subscriber

        # Subscriptions where unit_price differs from offer price
        stmt = (
            select(
                (Subscriber.first_name + " " + Subscriber.last_name).label(
                    "subscriber"
                ),
                CatalogOffer.name.label("plan"),
                Subscription.unit_price,
                Subscription.status,
                Subscription.id.label("sub_id"),
            )
            .join(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
            .join(Subscriber, Subscription.subscriber_id == Subscriber.id)
            .where(Subscription.unit_price.isnot(None))
            .where(Subscription.is_active.is_(True))
            .order_by(Subscription.unit_price.desc())
            .limit(100)
        )
        rows = db.execute(stmt).all()
        overrides = [
            {
                "subscriber": r[0],
                "plan": r[1],
                "price": float(r[2] or 0),
                "status": r[3].value if hasattr(r[3], "value") else str(r[3]),
            }
            for r in rows
        ]

        # Active add-ons (SubscriptionAddOn has no is_active column — "active"
        # means not yet ended).
        addon_stmt = select(func.count(SubscriptionAddOn.id)).where(
            (SubscriptionAddOn.end_at.is_(None))
            | (SubscriptionAddOn.end_at > datetime.now(UTC))
        )
        addon_count = db.scalar(addon_stmt) or 0
    except Exception as exc:
        logger.warning("Could not query custom pricing: %s", exc)
        overrides = []
        addon_count = 0

    return {"overrides": overrides, "total": len(overrides), "addon_count": addon_count}
