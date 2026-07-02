"""Service helpers for web/admin report routes."""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.network import IPAssignment, IPv4Address, IPv6Address
from app.models.subscriber import AccountStatus, Subscriber, SubscriberCategory
from app.services import billing as billing_service
from app.services import network as network_service
from app.services import provisioning as operations_service
from app.services import subscriber as subscriber_service

logger = logging.getLogger(__name__)


def _ensure_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _collect_pool_data(
    db: Session,
    pool_limit: int,
    block_limit: int,
) -> tuple[list[dict], int, int]:
    ip_pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=pool_limit,
        offset=0,
    )

    used_ips = 0
    total_ips = 0
    pool_data = []

    for pool in ip_pools:
        blocks = network_service.ip_blocks.list(
            db=db,
            pool_id=str(pool.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=block_limit,
            offset=0,
        )
        pool_used = 0
        pool_total = 0
        pool_ip_version = getattr(pool.ip_version, "value", pool.ip_version)
        if pool_ip_version == "ipv6":
            pool_total = (
                db.query(IPv6Address).filter(IPv6Address.pool_id == pool.id).count()
            )
            pool_used = (
                db.query(IPAssignment)
                .join(IPv6Address, IPAssignment.ipv6_address_id == IPv6Address.id)
                .filter(IPv6Address.pool_id == pool.id)
                .filter(IPAssignment.is_active.is_(True))
                .count()
            )
        else:
            pool_total = (
                db.query(IPv4Address).filter(IPv4Address.pool_id == pool.id).count()
            )
            pool_used = (
                db.query(IPAssignment)
                .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
                .filter(IPv4Address.pool_id == pool.id)
                .filter(IPAssignment.is_active.is_(True))
                .count()
            )

        if pool_total == 0:
            for _ in blocks:
                pool_total += 256
        pool_total = pool_total if pool_total > 0 else 256

        pool_data.append(
            {
                "name": pool.name,
                "cidr": pool.cidr,
                "used_count": pool_used,
                "total_count": pool_total,
            }
        )
        used_ips += pool_used
        total_ips += pool_total

    return pool_data, used_ips, total_ips


def get_network_report_data(db: Session, hours: int | None = None) -> dict:
    olts = network_service.olt_devices.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000 if hours else 100,
        offset=0,
    )
    total_olts = len(olts)
    active_olts = sum(1 for olt in olts if olt.is_active)

    onts = network_service.ont_units.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000 if hours else 1000,
        offset=0,
    )
    if hours:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        onts = [
            ont
            for ont in onts
            if (updated_at := _ensure_aware_datetime(ont.updated_at)) is not None
            and updated_at >= cutoff
        ]
    total_onts = len(onts)
    connected_onts = sum(1 for ont in onts if ont.is_active)

    recent_ont_activity = sorted(
        onts,
        key=lambda x: x.updated_at if x.updated_at else datetime.min,
        reverse=True,
    )[:10]

    pool_data, used_ips, total_ips = _collect_pool_data(
        db=db,
        pool_limit=5000 if hours else 100,
        block_limit=100,
    )
    ip_pool_usage = (used_ips / total_ips * 100) if total_ips > 0 else 0

    vlans = network_service.vlans.list(
        db=db,
        region_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000 if hours else 100,
        offset=0,
    )
    active_vlans = sum(1 for v in vlans if v.is_active)

    return {
        "olts": olts,
        "total_olts": total_olts,
        "active_olts": active_olts,
        "total_onts": total_onts,
        "connected_onts": connected_onts,
        "recent_ont_activity": recent_ont_activity,
        "pool_data": pool_data,
        "used_ips": used_ips,
        "total_ips": total_ips,
        "ip_pool_usage": ip_pool_usage,
        "active_vlans": active_vlans,
    }


def build_network_export_csv(data: dict, hours: int | None = None) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["metric", "value"])
    writer.writerow(["total_olts", data["total_olts"]])
    writer.writerow(["active_olts", data["active_olts"]])
    writer.writerow(["total_onts", data["total_onts"]])
    writer.writerow(["connected_onts", data["connected_onts"]])
    writer.writerow(["used_ips", data["used_ips"]])
    writer.writerow(["total_ips", data["total_ips"]])
    writer.writerow(["ip_pool_usage_percent", f"{data['ip_pool_usage']:.2f}"])
    writer.writerow(["active_vlans", data["active_vlans"]])
    writer.writerow(["report_window_hours", hours or ""])
    writer.writerow([])
    writer.writerow(["pool_name", "cidr", "used_count", "total_count", "usage_percent"])
    for pool in data["pool_data"]:
        usage = (
            (pool["used_count"] / pool["total_count"] * 100)
            if pool["total_count"]
            else 0
        )
        writer.writerow(
            [
                pool["name"],
                pool["cidr"],
                pool["used_count"],
                pool["total_count"],
                f"{usage:.2f}",
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def _derive_subscriber_status(subscriber: Subscriber) -> AccountStatus:
    if subscriber.status is not None:
        return subscriber.status
    return AccountStatus.active if subscriber.is_active else AccountStatus.canceled


def _date_range_values(
    *, date_from: str | None = None, date_to: str | None = None
) -> tuple[datetime | None, datetime | None, str, str]:
    from app.services.common import parse_date_filter

    start = parse_date_filter(date_from)
    parsed_to = parse_date_filter(date_to)
    end = parsed_to + timedelta(days=1) if parsed_to else None
    return start, end, date_from or "", date_to or ""


def _filter_subscribers_for_report(
    subscribers: list[Subscriber],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> list[Subscriber]:
    start, end, _, _ = _date_range_values(date_from=date_from, date_to=date_to)
    status_filter = (status or "").strip().lower()
    allowed_statuses = {item.value for item in AccountStatus}
    if status_filter and status_filter not in allowed_statuses:
        status_filter = ""

    filtered: list[Subscriber] = []
    for sub in subscribers:
        derived_status = _derive_subscriber_status(sub)
        sub.status = derived_status
        if status_filter and derived_status.value != status_filter:
            continue
        created_at = subscriber_service.get_effective_created_at(sub)
        if start and (created_at is None or created_at < start):
            continue
        if end and (created_at is None or created_at >= end):
            continue
        filtered.append(sub)
    return filtered


def _load_report_subscribers(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    limit: int = 5000,
) -> list[Subscriber]:
    start, end, _, _ = _date_range_values(date_from=date_from, date_to=date_to)
    stmt = (
        select(Subscriber)
        .where(subscriber_service.visible_subscriber_clause())
        .order_by(Subscriber.created_at.desc())
        .limit(limit)
    )
    if start is not None:
        stmt = stmt.where(Subscriber.created_at >= start)
    if end is not None:
        stmt = stmt.where(Subscriber.created_at < end)
    status_filter = (status or "").strip().lower()
    if status_filter in {item.value for item in AccountStatus}:
        stmt = stmt.where(Subscriber.status == AccountStatus(status_filter))
    return list(db.scalars(stmt).all())


def _customer_report_usage_window(
    *,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[datetime, datetime, str, str]:
    from app.services.web_reports_extended import _resolve_report_window

    return _resolve_report_window(
        date_from=date_from,
        date_to=date_to,
        days=days or 30,
    )


def _attach_period_usage_to_subscribers(
    db: Session,
    subscribers: list[Subscriber],
    *,
    start: datetime,
    end: datetime,
) -> float:
    from app.models.catalog import Subscription
    from app.services.web_reports_extended import _subscription_bandwidth_usage_subquery

    # Report-only fields stuffed onto ORM instances for template consumption.
    for sub in subscribers:
        sub.period_usage_gb = 0.0  # type: ignore[attr-defined]
        sub.period_avg_mbps = 0.0  # type: ignore[attr-defined]
        sub.period_active_services = 0  # type: ignore[attr-defined]

    subscriber_ids = [sub.id for sub in subscribers if getattr(sub, "id", None)]
    if not subscriber_ids:
        return 0.0

    span_seconds = max(0.0, (end - start).total_seconds())
    usage = _subscription_bandwidth_usage_subquery(start, end, span_seconds)
    rows = db.execute(
        select(
            Subscription.subscriber_id,
            func.coalesce(func.sum(usage.c.usage_bytes), 0).label("usage_bytes"),
            func.coalesce(func.sum(usage.c.avg_total), 0).label("avg_bps"),
            func.count(usage.c.subscription_id).label("active_services"),
        )
        .select_from(usage)
        .join(Subscription, Subscription.id == usage.c.subscription_id)
        .where(Subscription.subscriber_id.in_(subscriber_ids))
        .group_by(Subscription.subscriber_id)
    ).all()

    by_subscriber = {row.subscriber_id: row for row in rows}
    total_usage_gb = 0.0
    for sub in subscribers:
        row = by_subscriber.get(sub.id)
        if row is None:
            continue
        usage_gb = float(row.usage_bytes or 0) / (1024**3)
        sub.period_usage_gb = round(usage_gb, 2)  # type: ignore[attr-defined]
        sub.period_avg_mbps = round(  # type: ignore[attr-defined]
            float(row.avg_bps or 0) / 1_000_000, 2
        )
        sub.period_active_services = int(row.active_services or 0)  # type: ignore[attr-defined]
        total_usage_gb += usage_gb
    return round(total_usage_gb, 2)


def _invoice_amount_due(invoice: object) -> Decimal | int | float:
    for attr in ("balance_due", "amount_due", "total"):
        value = getattr(invoice, attr, None)
        if isinstance(value, (Decimal, int, float)):
            return value
    return 0


def _account_display_name(account: object | None) -> str:
    if not account:
        return ""
    organization = getattr(account, "organization", None)
    if organization is not None:
        return str(getattr(organization, "name", "") or "")
    name = f"{getattr(account, 'first_name', '')} {getattr(account, 'last_name', '')}".strip()
    if name:
        return name
    display_name = getattr(account, "display_name", None)
    if display_name:
        return str(display_name)
    return getattr(account, "account_number", "") or str(getattr(account, "id", ""))


def _payment_primary_invoice_id(payment) -> str | None:
    if not payment or not payment.allocations:
        return None
    allocation = min(
        payment.allocations,
        key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
    )
    return str(allocation.invoice_id)


def _percent_change(
    current: Decimal | int | float,
    previous: Decimal | int | float,
) -> float | None:
    if not previous:
        return None
    return round((float(current - previous) / float(previous)) * 100, 1)


def _month_starts(months: int = 6) -> list[datetime]:
    now = datetime.now(UTC)
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    starts = []
    year = first_this_month.year
    month = first_this_month.month - months + 1
    while month <= 0:
        month += 12
        year -= 1
    for _ in range(months):
        starts.append(datetime(year, month, 1, tzinfo=UTC))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return starts


def _monthly_payment_series(db: Session, *, months: int = 6) -> dict[str, list]:
    starts = _month_starts(months)
    labels: list[str] = []
    revenue: list[float] = []
    collected: list[float] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else datetime.now(UTC)
        label = start.strftime("%b")
        total = (
            db.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.is_active.is_(True),
                    Payment.status == PaymentStatus.succeeded,
                    Payment.paid_at >= start,
                    Payment.paid_at < end,
                )
            )
            or Decimal("0")
        )
        labels.append(label)
        revenue.append(float(total))
        collected.append(float(total))
    return {"labels": labels, "revenue": revenue, "collected": collected}


def _monthly_customer_growth_series(db: Session, *, months: int = 6) -> dict[str, list]:
    starts = _month_starts(months)
    labels: list[str] = []
    totals: list[int] = []
    new_counts: list[int] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else datetime.now(UTC)
        total = (
            db.scalar(
                select(func.count(Subscriber.id)).where(
                    subscriber_service.visible_subscriber_clause(),
                    Subscriber.created_at < end,
                )
            )
            or 0
        )
        new_count = (
            db.scalar(
                select(func.count(Subscriber.id)).where(
                    subscriber_service.visible_subscriber_clause(),
                    Subscriber.created_at >= start,
                    Subscriber.created_at < end,
                )
            )
            or 0
        )
        labels.append(start.strftime("%b"))
        totals.append(int(total))
        new_counts.append(int(new_count))
    return {"labels": labels, "total": totals, "new": new_counts}


def _monthly_churn_series(db: Session, *, months: int = 6) -> dict[str, list]:
    starts = _month_starts(months)
    labels: list[str] = []
    rates: list[float] = []
    counts: list[int] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else datetime.now(UTC)
        total = (
            db.scalar(
                select(func.count(Subscriber.id)).where(
                    subscriber_service.visible_subscriber_clause(),
                    Subscriber.created_at < end,
                )
            )
            or 0
        )
        cancelled = (
            db.scalar(
                select(func.count(Subscriber.id)).where(
                    subscriber_service.visible_subscriber_clause(),
                    Subscriber.status == AccountStatus.canceled,
                    Subscriber.updated_at >= start,
                    Subscriber.updated_at < end,
                )
            )
            or 0
        )
        labels.append(start.strftime("%b"))
        counts.append(int(cancelled))
        rates.append(round((int(cancelled) / int(total) * 100) if total else 0, 1))
    return {"labels": labels, "rate": rates, "count": counts}


def get_revenue_report_data(db: Session) -> dict:
    now = datetime.now(UTC)
    current_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_end = current_start
    previous_start = (
        current_start.replace(year=current_start.year - 1, month=12)
        if current_start.month == 1
        else current_start.replace(month=current_start.month - 1)
    )
    total_revenue = (
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.is_active.is_(True),
                Payment.status == PaymentStatus.succeeded,
            )
        )
        or Decimal("0")
    )
    current_revenue = (
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.is_active.is_(True),
                Payment.status == PaymentStatus.succeeded,
                Payment.paid_at >= current_start,
                Payment.paid_at < now,
            )
        )
        or Decimal("0")
    )
    previous_revenue = (
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.is_active.is_(True),
                Payment.status == PaymentStatus.succeeded,
                Payment.paid_at >= previous_start,
                Payment.paid_at < previous_end,
            )
        )
        or Decimal("0")
    )
    recent_payments = billing_service.payments.list(
        db=db,
        account_id=None,
        invoice_id=None,
        status=PaymentStatus.succeeded.value,
        is_active=None,
        order_by="paid_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    outstanding_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
    outstanding_row = db.execute(
        select(
            func.coalesce(func.sum(Invoice.balance_due), 0).label("amount"),
            func.count(Invoice.id).label("count"),
        ).where(
            Invoice.is_active.is_(True),
            Invoice.status.in_(outstanding_statuses),
            Invoice.balance_due > 0,
        )
    ).one()
    outstanding_amount = outstanding_row.amount or Decimal("0")
    outstanding_count = int(outstanding_row.count or 0)
    total_invoiced = (
        db.scalar(
            select(func.coalesce(func.sum(Invoice.total), 0)).where(
                Invoice.is_active.is_(True),
                Invoice.status != InvoiceStatus.void,
            )
        )
        or Decimal("0")
    )
    collection_rate = (
        (float(total_revenue) / float(total_invoiced) * 100) if total_invoiced else 0
    )
    try:
        from app.models.catalog import Subscription, SubscriptionStatus

        recurring_revenue = (
            db.scalar(
                select(func.coalesce(func.sum(Subscription.unit_price), 0)).where(
                    Subscription.status.in_(
                        [SubscriptionStatus.active, SubscriptionStatus.suspended]
                    )
                )
            )
            or Decimal("0")
        )
    except Exception:
        logger.debug("Failed to compute recurring revenue", exc_info=True)
        recurring_revenue = Decimal("0")
    revenue_data = _monthly_payment_series(db)
    revenue_growth = _percent_change(current_revenue, previous_revenue)
    if revenue_growth is None and current_revenue:
        revenue_growth = 0.0
    return {
        "total_revenue": total_revenue,
        "revenue_growth": revenue_growth,
        "recurring_revenue": recurring_revenue,
        "outstanding_amount": outstanding_amount,
        "outstanding_count": outstanding_count,
        "collection_rate": collection_rate,
        "recent_payments": recent_payments,
        "revenue_data": revenue_data,
    }


def _subscriber_growth_percent(db: Session) -> float | None:
    now = datetime.now(UTC)
    current_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_start = (
        current_start.replace(year=current_start.year - 1, month=12)
        if current_start.month == 1
        else current_start.replace(month=current_start.month - 1)
    )
    current_new = (
        db.scalar(
            select(func.count(Subscriber.id)).where(
                subscriber_service.visible_subscriber_clause(),
                Subscriber.created_at >= current_start,
                Subscriber.created_at < now,
            )
        )
        or 0
    )
    previous_new = (
        db.scalar(
            select(func.count(Subscriber.id)).where(
                subscriber_service.visible_subscriber_clause(),
                Subscriber.created_at >= previous_start,
                Subscriber.created_at < current_start,
            )
        )
        or 0
    )
    return _percent_change(current_new, previous_new)


def build_revenue_export_csv(db: Session, days: int | None = None) -> str:
    payments = billing_service.payments.list(
        db=db,
        account_id=None,
        invoice_id=None,
        status=None,
        is_active=None,
        order_by="paid_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        payments = [p for p in payments if p.paid_at and p.paid_at >= cutoff]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "paid_at",
            "account",
            "account_id",
            "invoice_id",
            "amount",
            "currency",
            "status",
            "payment_method",
            "provider",
        ]
    )
    for payment in payments:
        writer.writerow(
            [
                payment.paid_at.isoformat() if payment.paid_at else "",
                _account_display_name(payment.account),
                str(payment.account_id) if payment.account_id else "",
                _payment_primary_invoice_id(payment) or "",
                str(payment.amount or ""),
                payment.currency or "",
                payment.status.value if payment.status else "",
                payment.payment_method.name if payment.payment_method else "",
                payment.provider.name if payment.provider else "",
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def get_subscribers_report_data(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> dict:
    all_subscribers = _load_report_subscribers(
        db,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    usage_start, usage_end, usage_date_from, usage_date_to = (
        _customer_report_usage_window(date_from=date_from, date_to=date_to)
    )
    total_usage_gb = _attach_period_usage_to_subscribers(
        db,
        all_subscribers,
        start=usage_start,
        end=usage_end,
    )
    total_subscribers = len(all_subscribers)
    status_breakdown: dict[str, int] = {}
    active_count = 0
    suspended_count = 0
    for sub in all_subscribers:
        derived_status = _derive_subscriber_status(sub)
        sub.status = derived_status
        status_name = derived_status.value if derived_status else "unknown"
        status_breakdown[status_name] = status_breakdown.get(status_name, 0) + 1
        if derived_status == AccountStatus.active:
            active_count += 1
        elif derived_status == AccountStatus.suspended:
            suspended_count += 1
    active_rate = (
        (active_count / total_subscribers * 100) if total_subscribers > 0 else 0
    )
    recent_subscribers = sorted(
        all_subscribers,
        key=lambda x: (
            subscriber_service.get_effective_created_at(x)
            or datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True,
    )[:10]
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_this_month = len(
        [
            sub
            for sub in all_subscribers
            if (created_at := subscriber_service.get_effective_created_at(sub))
            is not None
            and created_at >= month_start
        ]
    )
    return {
        "total_subscribers": total_subscribers,
        "subscriber_growth": _subscriber_growth_percent(db),
        "new_this_month": new_this_month,
        "active_subscribers": active_count,
        "suspended_subscribers": suspended_count,
        "active_rate": active_rate,
        "status_breakdown": status_breakdown,
        "recent_subscribers": recent_subscribers,
        "customers": all_subscribers[:200],
        "date_from": date_from or "",
        "date_to": date_to or "",
        "usage_date_from": usage_date_from,
        "usage_date_to": usage_date_to,
        "total_usage_gb": total_usage_gb,
        "status_filter": status or "",
        "status_options": [item.value for item in AccountStatus],
        "growth_data": _monthly_customer_growth_series(db),
    }


def build_subscribers_export_csv(
    db: Session,
    days: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> str:
    all_subscribers = _load_report_subscribers(
        db,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        all_subscribers = [
            sub
            for sub in all_subscribers
            if (created_at := _ensure_aware_datetime(sub.created_at)) is not None
            and created_at >= cutoff
        ]
    usage_start, usage_end, _, _ = _customer_report_usage_window(
        days=days,
        date_from=date_from,
        date_to=date_to,
    )
    _attach_period_usage_to_subscribers(
        db,
        all_subscribers,
        start=usage_start,
        end=usage_end,
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "subscriber_id",
            "name",
            "type",
            "status",
            "created_at",
            "period_usage_gb",
            "period_avg_mbps",
            "period_active_services",
        ]
    )
    for sub in all_subscribers:
        derived_status = _derive_subscriber_status(sub)
        name = (
            sub.company_name
            if sub.category == SubscriberCategory.business
            else f"{sub.first_name} {sub.last_name}".strip()
            or sub.display_name
            or "Subscriber"
        )
        subscriber_type = (
            "organization" if sub.category == SubscriberCategory.business else "person"
        )
        writer.writerow(
            [
                str(sub.id),
                name,
                subscriber_type or "",
                derived_status.value if derived_status else "",
                (
                    created_at.isoformat()
                    if (created_at := subscriber_service.get_effective_created_at(sub))
                    is not None
                    else ""
                ),
                getattr(sub, "period_usage_gb", 0),
                getattr(sub, "period_avg_mbps", 0),
                getattr(sub, "period_active_services", 0),
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def get_churn_report_data(db: Session) -> dict:
    all_subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(subscriber_service.visible_subscriber_clause())
            .order_by(Subscriber.created_at.desc())
        ).all()
    )
    total_subscribers = len(all_subscribers)
    for sub in all_subscribers:
        sub.status = _derive_subscriber_status(sub)
    cancelled_subscribers = [
        s for s in all_subscribers if s.status == AccountStatus.canceled
    ]
    cancelled_count = len(cancelled_subscribers)
    at_risk_subscribers = [
        s for s in all_subscribers if s.status == AccountStatus.suspended
    ]
    at_risk_count = len(at_risk_subscribers)
    churn_rate = (
        (cancelled_count / total_subscribers * 100) if total_subscribers > 0 else 0
    )
    retention_rate = 100 - churn_rate
    recent_cancellations = sorted(
        cancelled_subscribers,
        key=lambda x: (
            subscriber_service.get_effective_updated_at(x)
            or datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True,
    )[:10]
    return {
        "churn_rate": churn_rate,
        "retention_rate": retention_rate,
        "cancelled_count": cancelled_count,
        "at_risk_count": at_risk_count,
        "churn_reasons": {},
        "churn_data": _monthly_churn_series(db),
        "recent_cancellations": recent_cancellations,
    }


def build_churn_export_csv(db: Session, days: int | None = None) -> str:
    all_subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=None,
        business_account_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    for sub in all_subscribers:
        sub.status = _derive_subscriber_status(sub)
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        all_subscribers = [
            sub
            for sub in all_subscribers
            if (updated_at := subscriber_service.get_effective_updated_at(sub))
            is not None
            and updated_at >= cutoff
        ]
    total_subscribers = len(all_subscribers)
    cancelled_subscribers = [
        sub for sub in all_subscribers if sub.status == AccountStatus.canceled
    ]
    at_risk_subscribers = [
        sub for sub in all_subscribers if sub.status == AccountStatus.suspended
    ]
    churn_rate = (
        (len(cancelled_subscribers) / total_subscribers * 100)
        if total_subscribers > 0
        else 0
    )
    retention_rate = 100 - churn_rate
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["metric", "value"])
    writer.writerow(["total_subscribers", total_subscribers])
    writer.writerow(["cancelled_count", len(cancelled_subscribers)])
    writer.writerow(["at_risk_count", len(at_risk_subscribers)])
    writer.writerow(["churn_rate_percent", f"{churn_rate:.2f}"])
    writer.writerow(["retention_rate_percent", f"{retention_rate:.2f}"])
    writer.writerow(["report_window_days", days or ""])
    writer.writerow([])
    writer.writerow(["subscriber_id", "name", "status", "updated_at"])
    for sub in cancelled_subscribers:
        name = (
            sub.company_name
            if sub.category == SubscriberCategory.business
            else f"{sub.first_name} {sub.last_name}".strip()
            or sub.display_name
            or "Subscriber"
        )
        writer.writerow(
            [
                str(sub.id),
                name,
                sub.status.value if sub.status else "",
                (
                    updated_at.isoformat()
                    if (updated_at := subscriber_service.get_effective_updated_at(sub))
                    is not None
                    else ""
                ),
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def get_technician_report_data(db: Session) -> dict:
    from sqlalchemy import func, select

    from app.models.provisioning import (
        AppointmentStatus,
        InstallAppointment,
        ProvisioningTask,
        ServiceOrder,
        ServiceOrderStatus,
        TaskStatus,
    )

    jobs_completed = (
        db.scalar(
            select(func.count(ServiceOrder.id)).where(
                ServiceOrder.status == ServiceOrderStatus.active
            )
        )
        or 0
    )
    recent_completions = list(
        db.scalars(
            select(ServiceOrder)
            .where(ServiceOrder.status == ServiceOrderStatus.active)
            .order_by(ServiceOrder.updated_at.desc())
            .limit(10)
        ).all()
    )

    # Real technician count from appointments + provisioning tasks
    tech_names: set[str] = set()
    appt_techs = db.scalars(
        select(InstallAppointment.technician)
        .where(InstallAppointment.technician.isnot(None))
        .distinct()
    ).all()
    tech_names.update(t for t in appt_techs if t)
    task_assignees = db.scalars(
        select(ProvisioningTask.assigned_to)
        .where(ProvisioningTask.assigned_to.isnot(None))
        .distinct()
    ).all()
    tech_names.update(t for t in task_assignees if t)
    total_technicians = len(tech_names)

    # Average completion hours from provisioning tasks with start/end times
    completed_tasks = db.execute(
        select(
            ProvisioningTask.started_at,
            ProvisioningTask.completed_at,
        ).where(
            ProvisioningTask.status == TaskStatus.completed,
            ProvisioningTask.started_at.isnot(None),
            ProvisioningTask.completed_at.isnot(None),
        )
    ).all()
    if completed_tasks:
        total_hours = 0.0
        counted = 0
        for t in completed_tasks:
            t_start = _ensure_aware_datetime(t.started_at)
            t_end = _ensure_aware_datetime(t.completed_at)
            if t_start and t_end:
                total_hours += max(0.0, (t_end - t_start).total_seconds() / 3600)
                counted += 1
        avg_completion_hours = round(total_hours / max(1, counted), 1)
    else:
        avg_completion_hours = 0.0

    # First visit rate from appointments (completed vs no-show/canceled)
    total_appointments = db.scalar(select(func.count(InstallAppointment.id))) or 0
    completed_appointments = (
        db.scalar(
            select(func.count(InstallAppointment.id)).where(
                InstallAppointment.status == AppointmentStatus.completed
            )
        )
        or 0
    )
    (
        db.scalar(
            select(func.count(InstallAppointment.id)).where(
                InstallAppointment.status == AppointmentStatus.no_show
            )
        )
        or 0
    )
    if total_appointments > 0:
        first_visit_rate = round((completed_appointments / total_appointments) * 100, 1)
    else:
        first_visit_rate = 0.0

    # Per-technician stats
    technician_stats: list[dict[str, object]] = []
    for tech_name in sorted(tech_names):
        tech_total = (
            db.scalar(
                select(func.count(InstallAppointment.id)).where(
                    InstallAppointment.technician == tech_name
                )
            )
            or 0
        )
        tech_completed = (
            db.scalar(
                select(func.count(InstallAppointment.id)).where(
                    InstallAppointment.technician == tech_name,
                    InstallAppointment.status == AppointmentStatus.completed,
                )
            )
            or 0
        )
        technician_stats.append(
            {
                "name": tech_name,
                "total_jobs": tech_total,
                "completed_jobs": tech_completed,
                "avg_hours": avg_completion_hours,
                "rating": round(
                    (tech_completed / tech_total * 5) if tech_total > 0 else 0, 1
                ),
            }
        )

    job_type_rows = db.execute(
        select(ServiceOrder.status, func.count(ServiceOrder.id)).group_by(
            ServiceOrder.status
        )
    ).all()
    job_type_breakdown = {
        (row.status.value if row.status else "unknown"): int(row[1] or 0)
        for row in job_type_rows
    }

    return {
        "total_technicians": total_technicians,
        "jobs_completed": jobs_completed,
        "avg_completion_hours": avg_completion_hours,
        "first_visit_rate": first_visit_rate,
        "technician_stats": technician_stats[:10],
        "job_type_breakdown": job_type_breakdown,
        "recent_completions": recent_completions,
    }


def build_technician_export_csv(db: Session, days: int | None = None) -> str:
    report_data = get_technician_report_data(db)
    technician_stats = list(report_data.get("technician_stats") or [])
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "technician",
            "total_jobs",
            "completed_jobs",
            "avg_completion_hours",
            "rating",
            "jobs_completed_total",
            "report_window_days",
        ]
    )
    jobs_completed = report_data.get("jobs_completed", 0)
    for tech in technician_stats:
        writer.writerow(
            [
                tech["name"],
                tech["total_jobs"],
                tech["completed_jobs"],
                tech["avg_hours"],
                tech["rating"],
                jobs_completed,
                days or "",
            ]
        )
    content = output.getvalue()
    output.close()
    return content
