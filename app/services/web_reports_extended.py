"""Extended report service functions for 04_administration features."""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services import subscriber as subscriber_service
from app.services.common import parse_date_filter as _parse_date

logger = logging.getLogger(__name__)


def _bandwidth_total_bps_expr():
    from app.models.bandwidth import BandwidthSample

    return cast(BandwidthSample.rx_bps, BigInteger) + cast(
        BandwidthSample.tx_bps,
        BigInteger,
    )


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


def _usage_bytes_expr(avg_bps, span_seconds: float):
    return avg_bps / 8.0 * span_seconds


def _subscription_bandwidth_usage_subquery(
    start: datetime, end: datetime, span_seconds: float
):
    from app.models.bandwidth import BandwidthSample

    total_bps = _bandwidth_total_bps_expr()
    avg_total = func.avg(total_bps)
    return (
        select(
            BandwidthSample.subscription_id.label("subscription_id"),
            func.avg(BandwidthSample.rx_bps).label("avg_rx"),
            func.avg(BandwidthSample.tx_bps).label("avg_tx"),
            func.max(BandwidthSample.rx_bps).label("peak_rx"),
            func.max(BandwidthSample.tx_bps).label("peak_tx"),
            avg_total.label("avg_total"),
            _usage_bytes_expr(avg_total, span_seconds).label("usage_bytes"),
        )
        .where(
            BandwidthSample.sample_at >= start,
            BandwidthSample.sample_at < end,
        )
        .group_by(BandwidthSample.subscription_id)
        .subquery()
    )


# ---------------------------------------------------------------------------
# 4.25 Subscriber Growth Chart
# ---------------------------------------------------------------------------


def get_subscriber_growth_data(db: Session, *, days: int = 30) -> dict:
    """Daily subscriber counts by status over last N days."""
    from app.models.subscriber import SubscriberStatus

    end = datetime.now(UTC)
    start = end - timedelta(days=days)

    visible_subscribers = [
        SimpleNamespace(
            status=row.status,
            metadata_=row.metadata_,
            splynx_customer_id=row.splynx_customer_id,
            account_start_date=row.account_start_date,
            created_at=row.created_at,
        )
        for row in db.execute(
            select(
                Subscriber.status,
                Subscriber.metadata_,
                Subscriber.splynx_customer_id,
                Subscriber.account_start_date,
                Subscriber.created_at,
            ).where(subscriber_service.visible_subscriber_clause())
        ).all()
    ]
    total = len(visible_subscribers)

    # Count by status
    status_counts: dict[str, int] = {}
    for s in SubscriberStatus:
        count = sum(1 for row in visible_subscribers if row.status == s)
        status_counts[s.value] = count

    # New this month
    month_start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_this_month = sum(
        1
        for row in visible_subscribers
        if (
            (created_at := subscriber_service.get_effective_created_at(row)) is not None
            and created_at >= month_start
        )
    )

    # Daily chart data — cumulative subscriber count per day
    chart_labels = []
    chart_data = []
    for i in range(days):
        day = start + timedelta(days=i)
        chart_labels.append(day.strftime("%Y-%m-%d"))
        day_count = sum(
            1
            for row in visible_subscribers
            if (
                (created_at := subscriber_service.get_effective_created_at(row))
                is not None
                and created_at <= day
            )
        )
        chart_data.append(day_count)

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
            .join(
                InvoiceLine,
                InvoiceLine.subscription_id == Subscription.id,
                isouter=True,
            )
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
            .group_by(
                Subscriber.id,
                (Subscriber.first_name + " " + Subscriber.last_name).label("full_name"),
            )
            .order_by(
                (Subscriber.first_name + " " + Subscriber.last_name).label("full_name")
            )
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

        stmt = (
            select(Invoice)
            .where(Invoice.tax_total > 0)
            .order_by(Invoice.issued_at.desc())
            .limit(200)
        )
        invoices = list(db.scalars(stmt).all())
    except Exception as exc:
        logger.warning("Could not query tax data: %s", exc)
        invoices = []

    total_tax = sum(getattr(i, "tax_total", 0) or 0 for i in invoices)
    return {"invoices": invoices, "total_tax": total_tax}


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
    """Monthly recurring revenue movement with real subscription data."""
    from app.models.catalog import Subscription, SubscriptionStatus

    parsed_from = _parse_date(date_from)
    parsed_to = _parse_date(date_to)
    if parsed_from is not None:
        year = parsed_from.year
    if not year:
        year = datetime.now(UTC).year
    range_start = parsed_from
    range_end = parsed_to + timedelta(days=1) if parsed_to is not None else None

    months = []
    now = datetime.now(UTC)

    for m in range(1, 13):
        month_start = datetime(year, m, 1, tzinfo=UTC)
        if m < 12:
            month_end = datetime(year, m + 1, 1, tzinfo=UTC)
        else:
            month_end = datetime(year + 1, 1, 1, tzinfo=UTC)

        if range_start is not None and month_end <= range_start:
            continue
        if range_end is not None and month_start >= range_end:
            continue

        # Skip future months
        if month_start > now:
            months.append(
                {
                    "month": f"{year}-{m:02d}",
                    "start_count": 0,
                    "new": 0,
                    "cancellations": 0,
                    "end_count": 0,
                    "net_change": 0,
                }
            )
            continue

        # Active at start of month (created before month_start and not canceled before)
        start_count = (
            db.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.created_at < month_start,
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.active,
                            SubscriptionStatus.suspended,
                            SubscriptionStatus.pending,
                        ]
                    ),
                )
            )
            or 0
        )

        # New subscriptions created this month
        new_count = (
            db.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.created_at >= month_start,
                    Subscription.created_at < month_end,
                )
            )
            or 0
        )

        # Cancellations this month
        cancel_count = (
            db.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.status == SubscriptionStatus.canceled,
                    Subscription.updated_at >= month_start,
                    Subscription.updated_at < month_end,
                )
            )
            or 0
        )

        end_count = start_count + new_count - cancel_count

        months.append(
            {
                "month": f"{year}-{m:02d}",
                "start_count": start_count,
                "new": new_count,
                "cancellations": cancel_count,
                "end_count": max(0, end_count),
                "net_change": new_count - cancel_count,
            }
        )

    total = (
        db.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status.in_(
                    [
                        SubscriptionStatus.active,
                        SubscriptionStatus.suspended,
                    ]
                ),
            )
        )
        or 0
    )

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
    """Network usage analytics — total usage, per-plan, top consumers."""
    from app.models.catalog import CatalogOffer, Subscription
    from app.models.subscriber import Subscriber

    start, end, date_from_value, date_to_value = _resolve_report_window(
        date_from=date_from,
        date_to=date_to,
        days=days,
    )
    span_seconds = max(0.0, (end - start).total_seconds())
    usage = _subscription_bandwidth_usage_subquery(start, end, span_seconds)

    row = db.execute(
        select(
            func.coalesce(func.sum(usage.c.usage_bytes), 0).label("usage_bytes"),
            func.coalesce(func.sum(usage.c.avg_rx), 0).label("avg_rx"),
            func.coalesce(func.sum(usage.c.avg_tx), 0).label("avg_tx"),
            func.max(usage.c.peak_rx).label("peak_rx"),
            func.max(usage.c.peak_tx).label("peak_tx"),
            func.count(usage.c.subscription_id).label("active_subs"),
        )
    ).first()

    usage_bytes = float(row.usage_bytes or 0) if row else 0
    avg_rx = float(row.avg_rx or 0) if row else 0
    avg_tx = float(row.avg_tx or 0) if row else 0
    peak_rx = float(row.peak_rx or 0) if row else 0
    peak_tx = float(row.peak_tx or 0) if row else 0
    active_subs = int(row.active_subs or 0) if row else 0
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

    plan_rows = db.execute(
        select(
            CatalogOffer.name.label("name"),
            func.coalesce(func.sum(usage.c.avg_total), 0).label("avg_bps"),
            func.coalesce(func.sum(usage.c.usage_bytes), 0).label("usage_bytes"),
            func.count(usage.c.subscription_id).label("sub_count"),
        )
        .select_from(usage)
        .join(Subscription, Subscription.id == usage.c.subscription_id, isouter=True)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id, isouter=True)
        .group_by(CatalogOffer.name)
        .order_by(func.coalesce(func.sum(usage.c.usage_bytes), 0).desc())
    ).all()
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
    """Revenue segmented by offer service type and plan category."""
    try:
        from app.models.billing import Invoice, InvoiceLine
        from app.models.catalog import CatalogOffer, Subscription

        # Revenue by service_type
        stmt_service = (
            select(
                CatalogOffer.service_type,
                func.count(func.distinct(Invoice.id)).label("invoice_count"),
                func.coalesce(func.sum(InvoiceLine.amount), 0).label("total"),
            )
            .select_from(InvoiceLine)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .join(
                Subscription,
                Subscription.id == InvoiceLine.subscription_id,
                isouter=True,
            )
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id, isouter=True)
            .where(Invoice.is_active.is_(True))
            .group_by(CatalogOffer.service_type)
            .order_by(func.coalesce(func.sum(InvoiceLine.amount), 0).desc())
        )
        rows = db.execute(stmt_service).all()
        categories = [
            {
                "name": (
                    r[0].value
                    if hasattr(r[0], "value")
                    else str(r[0] or "Uncategorized")
                ),
                "invoice_count": r[1],
                "revenue": float(r[2] or 0),
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


# ---------------------------------------------------------------------------
# Custom Pricing & Discounts (subscription add-ons and unit price overrides)
# ---------------------------------------------------------------------------


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
