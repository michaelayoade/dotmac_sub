"""Service and usage flows for customer portal."""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, TypedDict
from uuid import UUID


class VacationHoldUsage(TypedDict):
    """Usage stats for vacation holds."""

    holds_this_year: int
    last_hold_date: datetime | None
    days_since_last: int | None


from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.bandwidth import BandwidthSample
from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.network import OntAssignment, OntUnit
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.usage import RadiusAccountingSession, UsageRecord
from app.services import catalog as catalog_service
from app.services import customer_portal_context
from app.services import provisioning as provisioning_service
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.customer_portal_flow_changes import (
    get_offer_price_summary,
    get_plan_change_copy,
)
from app.services.customer_portal_flow_common import (
    _compute_total_pages,
    _resolve_next_billing_date,
)
from app.services.web_network_ont_actions import config_setters as ont_config_setters
from app.services.web_network_ont_actions import device_actions as ont_device_actions

logger = logging.getLogger(__name__)

_PORTAL_VISIBLE_SERVICE_STATUSES = [
    SubscriptionStatus.pending,
    SubscriptionStatus.active,
    SubscriptionStatus.blocked,
    SubscriptionStatus.suspended,
    SubscriptionStatus.stopped,
    SubscriptionStatus.disabled,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
]


def _get_fup_status(
    db: Session, offer_id: str | None, subscription_id: str
) -> dict | None:
    """Get FUP status for a subscription's offer, if a policy exists."""
    if not offer_id:
        return None
    try:
        from app.services.fup import FupPolicies

        policy = FupPolicies.get_by_offer(db, offer_id)
        if not policy or not policy.is_active:
            return None

        # Compute current usage for this billing period
        now = datetime.now(UTC)
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
        period_start = now - timedelta(days=30)
        if subscription and subscription.next_billing_at:
            nba = _as_utc(subscription.next_billing_at) or now
            cycle_days = 30
            period_start = nba - timedelta(days=cycle_days)

        usage_gb = _usage_total_gb(
            db,
            subscription_id=subscription_id,
            start_at=period_start,
            end_at=now,
        )

        # Fallback to estimated traffic volume if accounting records are unavailable.
        if usage_gb <= 0:
            rows = (
                db.query(
                    func.avg(BandwidthSample.rx_bps).label("rx"),
                    func.avg(BandwidthSample.tx_bps).label("tx"),
                )
                .filter(
                    BandwidthSample.subscription_id == coerce_uuid(subscription_id),
                    BandwidthSample.sample_at >= period_start,
                    BandwidthSample.sample_at <= now,
                )
                .first()
            )
            if rows and (rows.rx or rows.tx):
                span_seconds = max(0, (now - period_start).total_seconds())
                total_bytes = (
                    (float(rows.rx or 0) + float(rows.tx or 0)) / 8.0
                ) * span_seconds
                usage_gb = total_bytes / (1024**3)

        # Get the data cap from the first active rule
        allowance_gb = None
        active_rules = [r for r in (policy.rules or []) if r.is_active]
        active_rules.sort(key=lambda r: r.sort_order or 0)
        if active_rules:
            from app.services.fup import _threshold_gb

            allowance_gb = max(_threshold_gb(rule) for rule in active_rules)

        usage_pct = 0.0
        if allowance_gb and allowance_gb > 0:
            usage_pct = min(100.0, (usage_gb / allowance_gb) * 100)

        # Determine status level
        status_level = "normal"
        if usage_pct >= 100:
            status_level = "exceeded"
        elif usage_pct >= 80:
            status_level = "warning"

        # Policy doesn't have a name field; use offer or generic label
        policy_label = "Fair Usage Policy"
        if hasattr(policy, "offer") and policy.offer and hasattr(policy.offer, "name"):
            policy_label = f"FUP — {policy.offer.name}"

        return {
            "policy_name": policy_label,
            "usage_gb": round(usage_gb, 2),
            "allowance_gb": round(allowance_gb, 2) if allowance_gb else None,
            "usage_pct": round(usage_pct, 1),
            "status_level": status_level,
            "rules_count": len(active_rules),
        }
    except Exception as exc:
        logger.warning("Failed to get FUP status for offer %s: %s", offer_id, exc)
        return None


def _usage_total_gb(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> float:
    total = (
        db.query(func.coalesce(func.sum(UsageRecord.total_gb), 0))
        .filter(
            UsageRecord.subscription_id == coerce_uuid(subscription_id),
            UsageRecord.recorded_at >= start_at,
            UsageRecord.recorded_at <= end_at,
        )
        .scalar()
    )
    return float(total or 0)


def _daily_usage_records(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, float]:
    bucket = func.date_trunc("day", UsageRecord.recorded_at)
    rows = (
        db.query(
            bucket.label("bucket_start"),
            func.coalesce(func.sum(UsageRecord.total_gb), 0).label("total_gb"),
        )
        .filter(
            UsageRecord.subscription_id == coerce_uuid(subscription_id),
            UsageRecord.recorded_at >= start_at,
            UsageRecord.recorded_at <= end_at,
        )
        .group_by(bucket)
        .all()
    )
    result: dict[Any, float] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        result[bucket_start.date()] = float(row.total_gb or 0)
    return result


def _daily_usage_breakdown_records(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, tuple[float, float, float]]:
    bucket = func.date_trunc("day", UsageRecord.recorded_at)
    rows = (
        db.query(
            bucket.label("bucket_start"),
            func.coalesce(func.sum(UsageRecord.input_gb), 0).label("download_gb"),
            func.coalesce(func.sum(UsageRecord.output_gb), 0).label("upload_gb"),
            func.coalesce(func.sum(UsageRecord.total_gb), 0).label("total_gb"),
        )
        .filter(
            UsageRecord.subscription_id == coerce_uuid(subscription_id),
            UsageRecord.recorded_at >= start_at,
            UsageRecord.recorded_at <= end_at,
        )
        .group_by(bucket)
        .all()
    )
    result: dict[Any, tuple[float, float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        result[bucket_start.date()] = (
            float(row.download_gb or 0),
            float(row.upload_gb or 0),
            float(row.total_gb or 0),
        )
    return result


def _daily_bandwidth_averages(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, tuple[float, float]]:
    bucket = func.date_trunc("day", BandwidthSample.sample_at)
    rows = (
        db.query(
            bucket.label("bucket_start"),
            func.avg(BandwidthSample.rx_bps).label("rx_bps"),
            func.avg(BandwidthSample.tx_bps).label("tx_bps"),
        )
        .filter(
            BandwidthSample.subscription_id == coerce_uuid(subscription_id),
            BandwidthSample.sample_at >= start_at,
            BandwidthSample.sample_at <= end_at,
        )
        .group_by(bucket)
        .all()
    )
    result: dict[Any, tuple[float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        result[bucket_start.date()] = (
            float(row.rx_bps or 0),
            float(row.tx_bps or 0),
        )
    return result


def _get_pppoe_credentials(db: Session, subscriber_id: str | None) -> dict | None:
    """Get PPPoE credentials for a subscriber, if any exist."""
    if not subscriber_id:
        return None
    try:
        from app.models.catalog import AccessCredential, ConnectionType

        cred = (
            db.query(AccessCredential)
            .filter(
                AccessCredential.subscriber_id == coerce_uuid(subscriber_id),
                AccessCredential.connection_type == ConnectionType.pppoe,
                AccessCredential.is_active.is_(True),
            )
            .first()
        )
        if not cred:
            return None
        return {
            "username": cred.username,
            "has_password": bool(cred.secret_hash),
        }
    except Exception as exc:
        logger.warning(
            "Failed to get PPPoE credentials for subscriber %s: %s", subscriber_id, exc
        )
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _usage_period_bounds(
    period: str,
    *,
    activated_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if str(period or "").lower() == "last":
        end = now - timedelta(days=30)
        start = end - timedelta(days=30)
        return start, end
    active_since = _as_utc(activated_at) or (now - timedelta(days=30))
    return min(active_since, now), now


def _resolve_usage_subscription_id(db: Session, customer: dict) -> str | None:
    subscription_id = customer.get("subscription_id")
    if subscription_id:
        return str(subscription_id)

    account_id, _ = customer_portal_context.resolve_customer_account(customer, db)
    if not account_id:
        return None
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.subscriber_id == UUID(str(account_id)),
            Subscription.status.in_(_PORTAL_VISIBLE_SERVICE_STATUSES),
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )
    return str(subscription.id) if subscription else None


def _daily_bandwidth_usage(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
    page: int,
    per_page: int,
) -> tuple[list[Any], int]:
    daily_records = _daily_bandwidth_usage_records(
        db,
        subscription_id=subscription_id,
        start_at=start_at,
        end_at=end_at,
    )
    total = len(daily_records)
    page_start = (page - 1) * per_page
    page_end = page_start + per_page
    records = daily_records[page_start:page_end]

    return records, int(total)


def _daily_bandwidth_usage_records(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> list[Any]:
    by_day_usage = _daily_usage_breakdown_records(
        db,
        subscription_id=subscription_id,
        start_at=start_at,
        end_at=end_at,
    )

    start_day_dt = _as_utc(start_at) or start_at
    end_day_dt = _as_utc(end_at) or end_at
    start_day = start_day_dt.date()
    end_day = end_day_dt.date()
    total_days = max(1, (end_day - start_day).days + 1)
    radius_by_day = (
        _daily_radius_accounting_usage(
            db,
            subscription_id=subscription_id,
            start_at=start_at,
            end_at=end_at,
        )
        if len(by_day_usage) < total_days
        else {}
    )
    bandwidth_by_day = (
        _daily_bandwidth_averages(
            db,
            subscription_id=subscription_id,
            start_at=start_at,
            end_at=end_at,
        )
        if len(by_day_usage) < total_days
        else {}
    )

    daily_records: list[Any] = []
    day = end_day
    while day >= start_day:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        effective_start = max(start_at, day_start)
        effective_end = min(end_at, day_end)
        span_seconds = max(0.0, (effective_end - effective_start).total_seconds())

        usage_breakdown = by_day_usage.get(day)
        if usage_breakdown is not None:
            download_gb, upload_gb, total_gb = usage_breakdown
        else:
            if day in bandwidth_by_day:
                rx_bps, tx_bps = bandwidth_by_day[day]
                # rx = NAS ingress = subscriber upload; tx = egress = download.
                download_gb = ((tx_bps / 8.0) * span_seconds) / (1024**3)
                upload_gb = ((rx_bps / 8.0) * span_seconds) / (1024**3)
                total_gb = download_gb + upload_gb
            else:
                # Fallback for days with no sampled bandwidth history. RADIUS
                # accounting is coarser, but it preserves real historical
                # usage instead of rendering prior active days as empty.
                download_gb, upload_gb, total_gb = radius_by_day.get(
                    day,
                    (0.0, 0.0, 0.0),
                )

        daily_records.append(
            SimpleNamespace(
                recorded_at=day_start,
                usage_type="Daily Usage",
                amount=total_gb,
                usage_amount=total_gb,
                download_amount=download_gb,
                upload_amount=upload_gb,
                unit="GB",
                description=f"Total usage for {day.isoformat()}",
            )
        )
        day -= timedelta(days=1)

    return daily_records


def _daily_radius_accounting_usage(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[Any, tuple[float, float, float]]:
    accounting_day = func.date_trunc(
        "day",
        func.coalesce(
            RadiusAccountingSession.session_end,
            RadiusAccountingSession.session_start,
        ),
    )
    accounting_time = func.coalesce(
        RadiusAccountingSession.session_end,
        RadiusAccountingSession.session_start,
    )

    rows = (
        db.query(
            accounting_day.label("bucket_start"),
            func.sum(func.coalesce(RadiusAccountingSession.input_octets, 0)).label(
                "download_octets"
            ),
            func.sum(func.coalesce(RadiusAccountingSession.output_octets, 0)).label(
                "upload_octets"
            ),
        )
        .filter(RadiusAccountingSession.subscription_id == coerce_uuid(subscription_id))
        .filter(accounting_time >= start_at)
        .filter(accounting_time <= end_at)
        .group_by(accounting_day)
        .all()
    )

    usage_by_day: dict[Any, tuple[float, float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(getattr(row, "bucket_start", None))
        if bucket_start is None:
            continue
        download_gb = float(getattr(row, "download_octets", 0) or 0) / (1024**3)
        upload_gb = float(getattr(row, "upload_octets", 0) or 0) / (1024**3)
        usage_by_day[bucket_start.date()] = (
            download_gb,
            upload_gb,
            download_gb + upload_gb,
        )
    return usage_by_day


def _zabbix_usage_records(graph: list[dict[str, Any]]) -> list[Any]:
    by_day: dict[datetime, dict[str, float]] = defaultdict(
        lambda: {"download_bytes": 0.0, "upload_bytes": 0.0}
    )
    for point in graph:
        day = datetime.fromtimestamp(int(point.get("timestamp") or 0), tz=UTC).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        by_day[day]["download_bytes"] += float(point.get("download_bytes") or 0)
        by_day[day]["upload_bytes"] += float(point.get("upload_bytes") or 0)

    return [
        SimpleNamespace(
            recorded_at=day,
            usage_type="Zabbix Bandwidth",
            amount=(totals["download_bytes"] + totals["upload_bytes"]) / (1024**3),
            usage_amount=(totals["download_bytes"] + totals["upload_bytes"])
            / (1024**3),
            download_amount=totals["download_bytes"] / (1024**3),
            upload_amount=totals["upload_bytes"] / (1024**3),
            unit="GB",
            description=f"Counter-derived usage for {day.date().isoformat()}",
        )
        for day, totals in sorted(by_day.items(), reverse=True)
    ]


def _serialize_usage_chart_records(records: list[Any]) -> list[dict[str, Any]]:
    ordered_records = sorted(
        records,
        key=lambda record: (
            _as_utc(getattr(record, "recorded_at", None))
            or datetime.min.replace(tzinfo=UTC)
        ),
    )

    chart_records: list[dict[str, Any]] = []
    for record in ordered_records:
        recorded_at = _as_utc(getattr(record, "recorded_at", None))
        if recorded_at is None:
            continue
        amount = float(
            getattr(record, "amount", None) or getattr(record, "usage_amount", 0) or 0
        )
        chart_records.append(
            {
                "label": recorded_at.strftime("%b %d"),
                "full_label": recorded_at.strftime("%b %d, %Y"),
                "value": round(amount, 2),
                "download_value": round(
                    float(getattr(record, "download_amount", 0) or 0),
                    2,
                ),
                "upload_value": round(
                    float(getattr(record, "upload_amount", 0) or 0),
                    2,
                ),
                "unit": str(getattr(record, "unit", None) or "GB"),
            }
        )
    return chart_records


def _usage_summary_stats(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, float]:
    subscription_uuid = coerce_uuid(subscription_id)
    avg_rx_bps, avg_tx_bps = db.query(
        func.avg(BandwidthSample.rx_bps),
        func.avg(BandwidthSample.tx_bps),
    ).filter(
        BandwidthSample.subscription_id == subscription_uuid,
        BandwidthSample.sample_at >= start_at,
        BandwidthSample.sample_at <= end_at,
    ).first() or (0, 0)

    bucket = func.date_trunc("day", BandwidthSample.sample_at)
    rows = (
        db.query(
            bucket.label("bucket_start"),
            func.avg(BandwidthSample.rx_bps).label("rx_bps"),
            func.avg(BandwidthSample.tx_bps).label("tx_bps"),
        )
        .filter(
            BandwidthSample.subscription_id == subscription_uuid,
            BandwidthSample.sample_at >= start_at,
            BandwidthSample.sample_at <= end_at,
        )
        .group_by(bucket)
        .all()
    )

    by_day: dict[Any, tuple[float, float]] = {}
    for row in rows:
        bucket_start = _as_utc(row.bucket_start)
        if bucket_start is None:
            continue
        by_day[bucket_start.date()] = (float(row.rx_bps or 0), float(row.tx_bps or 0))

    start_day_dt = _as_utc(start_at) or start_at
    end_day_dt = _as_utc(end_at) or end_at
    start_day = start_day_dt.date()
    end_day = end_day_dt.date()
    total_days = max(1, (end_day - start_day).days + 1)
    total_bytes = 0.0
    day = start_day
    while day <= end_day:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        effective_start = max(start_at, day_start)
        effective_end = min(end_at, day_end)
        span_seconds = max(0.0, (effective_end - effective_start).total_seconds())
        rx_bps, tx_bps = by_day.get(day, (0.0, 0.0))
        total_bytes += ((rx_bps + tx_bps) / 8.0) * span_seconds
        day += timedelta(days=1)

    avg_daily_usage_gb = (total_bytes / (1024**3)) / total_days
    # rx_bps/tx_bps are stored from the NAS-interface perspective: rx = NAS
    # ingress = subscriber UPLOAD, tx = NAS egress = subscriber DOWNLOAD. So the
    # subscriber-facing download is avg_tx and upload is avg_rx.
    average_download_mbps = float(avg_tx_bps or 0) / 1_000_000
    average_upload_mbps = float(avg_rx_bps or 0) / 1_000_000
    average_speed_mbps = average_download_mbps + average_upload_mbps

    return {
        "average_daily_usage_gb": avg_daily_usage_gb,
        "average_speed_mbps": average_speed_mbps,
        "average_download_mbps": average_download_mbps,
        "average_upload_mbps": average_upload_mbps,
    }


def get_usage_page(
    db: Session,
    customer: dict,
    period: str = "current",
    page: int = 1,
    per_page: int = 25,
    allow_postgres_fallback: bool = True,
) -> dict:
    """Get usage page data for the customer portal."""
    subscription_id_str = _resolve_usage_subscription_id(db, customer)

    empty_result: dict[str, Any] = {
        "usage_records": [],
        "chart_records": [],
        "period": period,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
        "usage_summary": {
            "average_daily_usage_gb": 0.0,
            "average_speed_mbps": 0.0,
            "average_download_mbps": 0.0,
            "average_upload_mbps": 0.0,
        },
        "fup_status": None,
        "usage_source": "none",
        "has_subscription": False,
    }
    if not subscription_id_str:
        return empty_result

    subscription = db.get(Subscription, coerce_uuid(subscription_id_str))
    activated_at = None
    if subscription:
        activated_at = _as_utc(getattr(subscription, "start_at", None)) or _as_utc(
            getattr(subscription, "created_at", None)
        )
    start_at, end_at = _usage_period_bounds(period, activated_at=activated_at)

    usage_source = "postgres"
    zabbix_usage = None
    chart_source_records: list[Any] = []
    if subscription:
        try:
            from app.services.zabbix_engine import get_zabbix_engine

            zabbix_usage = get_zabbix_engine().get_cached_customer_usage(
                subscription_id_str,
                period,
                page,
                per_page,
            )
        except Exception:
            logger.info(
                "customer_zabbix_usage_cache_fallback",
                extra={"event": "customer_zabbix_usage_cache_fallback"},
            )
            zabbix_usage = None

    if zabbix_usage:
        usage_records = zabbix_usage["usage_records"]
        total = int(zabbix_usage["total"])
        usage_summary = zabbix_usage["usage_summary"]
        usage_source = "zabbix"
        chart_source_records = _zabbix_usage_records(zabbix_usage.get("graph") or [])
    elif not allow_postgres_fallback:
        return {
            **empty_result,
            "period": period,
            "page": page,
            "per_page": per_page,
            "usage_source": "unavailable",
            "has_subscription": True,
        }
    else:
        chart_source_records = _daily_bandwidth_usage_records(
            db,
            subscription_id=subscription_id_str,
            start_at=start_at,
            end_at=end_at,
        )
        total = len(chart_source_records)
        page_start = (page - 1) * per_page
        page_end = page_start + per_page
        usage_records = chart_source_records[page_start:page_end]
        usage_summary = _usage_summary_stats(
            db,
            subscription_id=subscription_id_str,
            start_at=start_at,
            end_at=end_at,
        )

    # Resolve FUP status from subscriber's primary subscription offer
    fup_status = None
    if subscription and allow_postgres_fallback:
        fup_status = _get_fup_status(
            db,
            str(subscription.offer_id) if subscription.offer_id else None,
            subscription_id_str,
        )

    return {
        "usage_records": usage_records,
        "chart_records": _serialize_usage_chart_records(chart_source_records),
        "period": period,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
        "usage_summary": usage_summary,
        "fup_status": fup_status,
        "usage_source": usage_source,
        "has_subscription": True,
    }


def get_services_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get services page data for the customer portal."""
    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    empty_result: dict[str, Any] = {
        "services": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str:
        return empty_result

    query = (
        db.query(Subscription)
        .options(
            selectinload(Subscription.offer),
            selectinload(Subscription.offer_version),
        )
        .filter(
            Subscription.subscriber_id == coerce_uuid(account_id_str),
            Subscription.status.in_(_PORTAL_VISIBLE_SERVICE_STATUSES),
        )
    )
    if status:
        query = query.filter(
            Subscription.status == _validate_enum(status, SubscriptionStatus, "status")
        )

    services = (
        query.order_by(Subscription.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    stmt = select(func.count(Subscription.id)).where(
        Subscription.subscriber_id == coerce_uuid(account_id_str),
        Subscription.status.in_(_PORTAL_VISIBLE_SERVICE_STATUSES),
    )
    if status:
        stmt = stmt.where(
            Subscription.status == _validate_enum(status, SubscriptionStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "services": services,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_service_detail(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Get service detail data for the customer portal."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return None

    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None

    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)

    next_billing_date = _resolve_next_billing_date(db, subscription)
    copy = get_plan_change_copy(subscription)

    fup_status = _get_fup_status(
        db, str(current_offer.id) if current_offer else None, subscription_id
    )
    pppoe_creds = _get_pppoe_credentials(
        db, str(subscription.subscriber_id) if subscription else None
    )
    customer_assignment = _resolve_customer_subscription_assignment(db, subscription)
    customer_ont = customer_assignment.ont_unit if customer_assignment else None

    # Renewal context: show renewal banner when contract nearing expiration
    renewal_context: dict[str, Any] = {"show_renewal": False}
    if subscription.end_at:
        days_remaining = (subscription.end_at - datetime.now(UTC)).days
        if days_remaining <= 30 and subscription.status in (
            SubscriptionStatus.active,
            SubscriptionStatus.suspended,
        ):
            renewal_context = {
                "show_renewal": True,
                "days_remaining": max(days_remaining, 0),
                "end_date": subscription.end_at,
                "offer_id": str(current_offer.id) if current_offer else None,
                "offer_name": current_offer.name if current_offer else "Service",
            }

    # Billing mode for display
    billing_mode = "postpaid"
    if subscription.billing_mode:
        billing_mode = subscription.billing_mode.value

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "current_offer_summary": get_offer_price_summary(current_offer),
        "next_billing_date": next_billing_date,
        "fup_status": fup_status,
        "pppoe_credentials": pppoe_creds,
        "customer_ont": customer_ont,
        "customer_wifi_ssid": getattr(customer_assignment, "wifi_ssid", None),
        "can_reboot_ont": bool(
            customer_ont is not None
            and subscription.status == SubscriptionStatus.active
        ),
        "can_update_wifi": bool(
            customer_ont is not None
            and subscription.status == SubscriptionStatus.active
        ),
        "billing_mode": billing_mode,
        "billing_mode_display": "Prepaid" if billing_mode == "prepaid" else "Postpaid",
        **renewal_context,
        **copy,
    }


def _ont_reboot_cooldown_remaining(db: Session, ont_id: str) -> int:
    """Seconds until this ONT may be customer-rebooted again, 0 if allowed.

    Counts any recent reboot operation against the device (admin- or
    customer-initiated) — the device-side disruption is the same either way.
    """
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import resolve_value

    cooldown_val = resolve_value(
        db, SettingDomain.network, "customer_ont_reboot_cooldown_seconds"
    )
    try:
        cooldown_seconds = int(str(cooldown_val)) if cooldown_val is not None else 300
    except (TypeError, ValueError):
        cooldown_seconds = 300
    if cooldown_seconds <= 0:
        return 0

    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationTargetType,
        NetworkOperationType,
    )

    last = (
        db.query(NetworkOperation.created_at)
        .filter(NetworkOperation.operation_type == NetworkOperationType.ont_reboot)
        .filter(NetworkOperation.target_type == NetworkOperationTargetType.ont)
        .filter(NetworkOperation.target_id == coerce_uuid(ont_id))
        .order_by(NetworkOperation.created_at.desc())
        .first()
    )
    if not last or not last.created_at:
        return 0
    last_at = last.created_at
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - last_at).total_seconds()
    return max(0, int(cooldown_seconds - elapsed))


def reboot_customer_subscription_ont(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> tuple[bool, str]:
    """Reboot the active ONT associated with a customer's subscription."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return False, "Subscription not found"

    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return False, "Subscription not found"
    if subscription.status != SubscriptionStatus.active:
        return False, "Only active services can be rebooted"

    ont = _resolve_customer_subscription_ont(db, subscription)
    if ont is None:
        return False, "No active ONT is linked to this service"

    remaining = _ont_reboot_cooldown_remaining(db, str(ont.id))
    if remaining > 0:
        minutes = max(1, -(-remaining // 60))
        return False, (
            "Your device was restarted recently. Please wait about "
            f"{minutes} minute{'s' if minutes != 1 else ''} before trying again."
        )

    actor = f"customer:{customer.get('id') or customer.get('account_id') or account_id}"
    result = ont_device_actions.execute_reboot(
        db,
        str(ont.id),
        initiated_by=actor,
        request=None,
    )
    return bool(result.success), str(result.message or "Reboot request submitted")


def update_customer_subscription_wifi(
    db: Session,
    customer: dict,
    subscription_id: str,
    *,
    ssid: str,
    password: str | None = None,
    password_confirm: str | None = None,
) -> tuple[bool, str]:
    """Update customer-managed WiFi SSID/password for a linked active ONT."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return False, "Subscription not found"
    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return False, "Subscription not found"
    if subscription.status != SubscriptionStatus.active:
        return False, "Only active services can update WiFi settings"

    ssid_value = str(ssid or "").strip()
    password_value = str(password or "").strip()
    password_confirm_value = str(password_confirm or "").strip()
    if not ssid_value or len(ssid_value) > 32:
        return False, "WiFi name must be 1-32 characters"
    if password_value or password_confirm_value:
        if password_value != password_confirm_value:
            return False, "WiFi passwords do not match"
        if len(password_value) < 8:
            return False, "WiFi password must be at least 8 characters"

    ont = _resolve_customer_subscription_ont(db, subscription)
    if ont is None:
        return False, "No active ONT is linked to this service"

    result = ont_config_setters.set_wifi_config(
        db,
        str(ont.id),
        ssid=ssid_value,
        password=password_value or None,
        request=None,
    )
    return bool(result.success), str(result.message or "WiFi update submitted")


def _resolve_customer_subscription_ont(
    db: Session,
    subscription: Subscription,
) -> OntUnit | None:
    """Resolve the active ONT currently assigned to a subscription's subscriber."""
    assignment = _resolve_customer_subscription_assignment(db, subscription)
    return assignment.ont_unit if assignment else None


def _resolve_customer_subscription_assignment(
    db: Session,
    subscription: Subscription,
) -> OntAssignment | None:
    """Resolve the active ONT assignment for a subscription's subscriber."""
    if not subscription.subscriber_id:
        return None
    return db.scalars(
        select(OntAssignment)
        .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
        .where(OntAssignment.subscriber_id == subscription.subscriber_id)
        .where(OntAssignment.active.is_(True))
        .where(OntUnit.is_active.is_(True))
        .order_by(OntAssignment.assigned_at.desc().nullslast())
        .limit(1)
    ).first()


def get_service_orders_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get service orders page data for the customer portal."""
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    empty_result: dict[str, Any] = {
        "service_orders": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str and not subscription_id_str:
        return empty_result

    service_orders = provisioning_service.service_orders.list(
        db=db,
        subscriber_id=account_id_str,
        subscription_id=subscription_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = select(func.count(ServiceOrder.id))
    if account_id_str:
        stmt = stmt.where(ServiceOrder.subscriber_id == coerce_uuid(account_id_str))
    if subscription_id_str:
        stmt = stmt.where(
            ServiceOrder.subscription_id == coerce_uuid(subscription_id_str)
        )
    if status:
        stmt = stmt.where(
            ServiceOrder.status == _validate_enum(status, ServiceOrderStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "service_orders": service_orders,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_service_order_detail(
    db: Session,
    customer: dict,
    service_order_id: str,
) -> dict | None:
    """Get service order detail data for the customer portal."""
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    service_order = provisioning_service.service_orders.get(
        db=db, entity_id=service_order_id
    )
    if not service_order:
        return None

    so_subscriber = str(getattr(service_order, "subscriber_id", ""))
    so_subscription = str(getattr(service_order, "subscription_id", ""))
    if not account_id_str and not subscription_id_str:
        return None
    if (account_id_str and so_subscriber != account_id_str) or (
        subscription_id_str and so_subscription != subscription_id_str
    ):
        return None

    appointments = provisioning_service.install_appointments.list(
        db=db,
        service_order_id=service_order_id,
        status=None,
        order_by="scheduled_start",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    provisioning_tasks = provisioning_service.provisioning_tasks.list(
        db=db,
        service_order_id=service_order_id,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    return {
        "service_order": service_order,
        "appointments": appointments,
        "provisioning_tasks": provisioning_tasks,
    }


def get_installation_detail(
    db: Session,
    customer: dict,
    appointment_id: str,
) -> dict | None:
    """Get installation appointment detail data for the customer portal."""
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    appointment = provisioning_service.install_appointments.get(
        db=db, entity_id=appointment_id
    )
    if not appointment:
        return None

    service_order = provisioning_service.service_orders.get(
        db=db, entity_id=str(appointment.service_order_id)
    )
    if not service_order:
        return None

    so_subscriber = str(getattr(service_order, "subscriber_id", ""))
    so_subscription = str(getattr(service_order, "subscription_id", ""))
    if not account_id_str and not subscription_id_str:
        return None
    if (account_id_str and so_subscriber != account_id_str) or (
        subscription_id_str and so_subscription != subscription_id_str
    ):
        return None

    return {
        "appointment": appointment,
        "service_order": service_order,
    }


__all__ = [
    "get_usage_page",
    "get_services_page",
    "get_service_detail",
    "get_service_orders_page",
    "get_service_order_detail",
    "get_installation_detail",
    "get_suspend_page",
    "apply_service_suspend",
    "get_resume_page",
    "apply_service_resume",
]


def _get_vacation_hold_usage(
    db: Session,
    subscription_id: str,
) -> VacationHoldUsage:
    """Get vacation hold usage stats for a subscription this calendar year."""
    from app.models.enforcement_lock import EnforcementLock, EnforcementReason

    current_year = datetime.now(UTC).year
    year_start = datetime(current_year, 1, 1, tzinfo=UTC)

    # Count holds created this year (both active and resolved)
    holds_this_year = (
        db.query(EnforcementLock)
        .filter(EnforcementLock.subscription_id == subscription_id)
        .filter(EnforcementLock.reason == EnforcementReason.customer_hold)
        .filter(EnforcementLock.created_at >= year_start)
        .count()
    )

    # Get most recent hold (active or resolved) for cooldown calculation
    last_hold = (
        db.query(EnforcementLock)
        .filter(EnforcementLock.subscription_id == subscription_id)
        .filter(EnforcementLock.reason == EnforcementReason.customer_hold)
        .order_by(EnforcementLock.created_at.desc())
        .first()
    )

    last_hold_date: datetime | None = last_hold.created_at if last_hold else None
    days_since_last: int | None = None
    if last_hold_date:
        # Handle timezone-naive datetimes (e.g., from SQLite in tests)
        now = datetime.now(UTC)
        if last_hold_date.tzinfo is None:
            last_hold_date = last_hold_date.replace(tzinfo=UTC)
        days_since_last = (now - last_hold_date).days

    return {
        "holds_this_year": holds_this_year,
        "last_hold_date": last_hold_date,
        "days_since_last": days_since_last,
    }


def get_suspend_page(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Build context for the vacation hold confirmation page."""
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import resolve_value

    enabled = resolve_value(db, SettingDomain.catalog, "customer_suspend_enabled")
    if enabled is False:
        return None

    max_suspend_days = resolve_value(db, SettingDomain.catalog, "max_suspend_days")
    max_days = (
        int(max_suspend_days) if isinstance(max_suspend_days, (str, int, float)) else 30
    )

    account_id = customer.get("account_id")
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        return None

    if subscription.status != SubscriptionStatus.active:
        return None

    # Get usage limits and current usage
    max_holds_val = resolve_value(
        db, SettingDomain.catalog, "max_suspend_holds_per_year"
    )
    max_holds_per_year = (
        int(max_holds_val) if isinstance(max_holds_val, (str, int, float)) else 0
    )
    cooldown_val = resolve_value(db, SettingDomain.catalog, "suspend_cooldown_days")
    cooldown_days = (
        int(cooldown_val) if isinstance(cooldown_val, (str, int, float)) else 0
    )

    usage = _get_vacation_hold_usage(db, subscription_id)
    holds_remaining = None
    if max_holds_per_year > 0:
        holds_remaining = max(0, max_holds_per_year - usage["holds_this_year"])

    # Check if user can actually suspend (usage limits)
    can_suspend = True
    block_reason = None

    if max_holds_per_year > 0 and usage["holds_this_year"] >= max_holds_per_year:
        can_suspend = False
        block_reason = f"You have reached the maximum of {max_holds_per_year} vacation holds per year."
    elif cooldown_days > 0 and usage["days_since_last"] is not None:
        if usage["days_since_last"] < cooldown_days:
            can_suspend = False
            days_remaining = cooldown_days - usage["days_since_last"]
            block_reason = f"Please wait {days_remaining} more day(s) before using another vacation hold."

    offer = subscription.offer
    return {
        "subscription": subscription,
        "offer_name": offer.name if offer else "Service",
        "billing_mode": subscription.billing_mode.value
        if subscription.billing_mode
        else "postpaid",
        "max_days": max_days,
        "holds_this_year": usage["holds_this_year"],
        "holds_remaining": holds_remaining,
        "max_holds_per_year": max_holds_per_year if max_holds_per_year > 0 else None,
        "cooldown_days": cooldown_days if cooldown_days > 0 else None,
        "days_since_last": usage["days_since_last"],
        "can_suspend": can_suspend,
        "block_reason": block_reason,
    }


def apply_service_suspend(
    db: Session,
    customer: dict,
    subscription_id: str,
    days: int,
) -> dict:
    """Apply a customer-initiated vacation hold on a subscription."""
    from app.models.domain_settings import SettingDomain
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import suspend_subscription
    from app.services.settings_spec import resolve_value

    enabled = resolve_value(db, SettingDomain.catalog, "customer_suspend_enabled")
    if enabled is False:
        raise ValueError("Self-service suspension is not enabled")

    max_suspend_days = resolve_value(db, SettingDomain.catalog, "max_suspend_days")
    max_days = (
        int(max_suspend_days) if isinstance(max_suspend_days, (str, int, float)) else 30
    )
    if days < 1 or days > max_days:
        raise ValueError(f"Suspension must be between 1 and {max_days} days")

    account_id = customer.get("account_id")
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription not found")

    if subscription.status != SubscriptionStatus.active:
        raise ValueError("Only active subscriptions can be suspended")

    # Check usage limits
    max_holds_val = resolve_value(
        db, SettingDomain.catalog, "max_suspend_holds_per_year"
    )
    max_holds_per_year = (
        int(max_holds_val) if isinstance(max_holds_val, (str, int, float)) else 0
    )
    cooldown_val = resolve_value(db, SettingDomain.catalog, "suspend_cooldown_days")
    cooldown_days = (
        int(cooldown_val) if isinstance(cooldown_val, (str, int, float)) else 0
    )

    usage = _get_vacation_hold_usage(db, subscription_id)

    if max_holds_per_year > 0 and usage["holds_this_year"] >= max_holds_per_year:
        raise ValueError(
            f"You have reached the maximum of {max_holds_per_year} vacation holds per year"
        )

    if cooldown_days > 0 and usage["days_since_last"] is not None:
        if usage["days_since_last"] < cooldown_days:
            days_remaining = cooldown_days - usage["days_since_last"]
            raise ValueError(
                f"Please wait {days_remaining} more day(s) before using another vacation hold"
            )

    subscriber_id = str(subscription.subscriber_id)
    lock = suspend_subscription(
        db,
        subscription_id,
        reason=EnforcementReason.customer_hold,
        source=f"customer_portal:vacation_hold:{subscriber_id}",
        notes=f"Customer-initiated vacation hold for {days} days",
    )

    # Set scheduled auto-resume date
    resume_at = datetime.now(UTC) + timedelta(days=days)
    lock.resume_at = resume_at

    db.flush()

    logger.info(
        "Customer %s suspended subscription %s for %d days (vacation hold, resume_at=%s)",
        subscriber_id,
        subscription_id,
        days,
        resume_at.isoformat(),
    )

    return {
        "subscription_id": subscription_id,
        "days": days,
        "lock_id": str(lock.id),
        "resume_at": resume_at.isoformat(),
    }


def get_resume_page(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Build context for the resume service confirmation page."""
    from app.models.domain_settings import SettingDomain
    from app.models.enforcement_lock import EnforcementLock, EnforcementReason
    from app.services.settings_spec import resolve_value

    enabled = resolve_value(db, SettingDomain.catalog, "customer_suspend_enabled")
    if enabled is False:
        return None

    account_id = customer.get("account_id")
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        return None

    # Only allow resume for suspended subscriptions with customer_hold lock
    if subscription.status != SubscriptionStatus.suspended:
        return None

    # Check for active customer_hold lock
    lock = (
        db.query(EnforcementLock)
        .filter(
            EnforcementLock.subscription_id == coerce_uuid(subscription_id),
            EnforcementLock.reason == EnforcementReason.customer_hold,
            EnforcementLock.is_active.is_(True),
        )
        .first()
    )
    if not lock:
        # No customer-initiated hold found - cannot self-service resume
        return None

    offer = subscription.offer
    return {
        "subscription": subscription,
        "offer_name": offer.name if offer else "Service",
        "lock": lock,
        "suspended_since": lock.created_at,
        "resume_at": lock.resume_at,
    }


def apply_service_resume(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict:
    """Resume a customer-initiated vacation hold on a subscription."""
    from app.models.domain_settings import SettingDomain
    from app.models.enforcement_lock import EnforcementLock, EnforcementReason
    from app.services.account_lifecycle import restore_subscription
    from app.services.settings_spec import resolve_value

    enabled = resolve_value(db, SettingDomain.catalog, "customer_suspend_enabled")
    if enabled is False:
        raise ValueError("Self-service suspension is not enabled")

    account_id = customer.get("account_id")
    subscription = db.get(Subscription, subscription_id)
    if not subscription or str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription not found")

    if subscription.status != SubscriptionStatus.suspended:
        raise ValueError("Subscription is not suspended")

    # Check for active customer_hold lock
    lock = (
        db.query(EnforcementLock)
        .filter(
            EnforcementLock.subscription_id == coerce_uuid(subscription_id),
            EnforcementLock.reason == EnforcementReason.customer_hold,
            EnforcementLock.is_active.is_(True),
        )
        .first()
    )
    if not lock:
        raise ValueError(
            "Cannot resume: no customer-initiated hold found. "
            "Please contact support if your service was suspended for another reason."
        )

    subscriber_id = str(subscription.subscriber_id)
    restored = restore_subscription(
        db,
        subscription_id,
        trigger="customer",
        resolved_by=f"customer_portal:resume:{subscriber_id}",
        reason=EnforcementReason.customer_hold,
        notes="Customer-initiated resume via portal",
    )

    db.flush()

    logger.info(
        "Customer %s resumed subscription %s (vacation hold lifted, restored=%s)",
        subscriber_id,
        subscription_id,
        restored,
    )

    return {
        "subscription_id": subscription_id,
        "restored": restored,
    }
