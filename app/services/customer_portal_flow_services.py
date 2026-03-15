"""Service and usage flows for customer portal."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.bandwidth import BandwidthSample
from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.services import catalog as catalog_service
from app.services import customer_portal_context
from app.services import provisioning as provisioning_service
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.customer_portal_flow_common import (
    _compute_total_pages,
    _resolve_next_billing_date,
)

logger = logging.getLogger(__name__)


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
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.pending]),
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
        .order_by(bucket.desc())
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

    daily_records: list[Any] = []
    day = end_day
    while day >= start_day:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        effective_start = max(start_at, day_start)
        effective_end = min(end_at, day_end)
        span_seconds = max(0.0, (effective_end - effective_start).total_seconds())

        rx_bps, tx_bps = by_day.get(day, (0.0, 0.0))
        total_bytes = ((rx_bps + tx_bps) / 8.0) * span_seconds
        total_gb = total_bytes / (1024 ** 3)

        daily_records.append(
            SimpleNamespace(
                recorded_at=day_start,
                usage_type="Daily Usage",
                amount=total_gb,
                usage_amount=total_gb,
                unit="GB",
                description=f"Total usage for {day.isoformat()}",
            )
        )
        day -= timedelta(days=1)

    total = len(daily_records)
    page_start = (page - 1) * per_page
    page_end = page_start + per_page
    records = daily_records[page_start:page_end]

    return records, int(total)


def _usage_summary_stats(
    db: Session,
    *,
    subscription_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, float]:
    subscription_uuid = coerce_uuid(subscription_id)
    avg_rx_bps, avg_tx_bps = (
        db.query(
            func.avg(BandwidthSample.rx_bps),
            func.avg(BandwidthSample.tx_bps),
        )
        .filter(
            BandwidthSample.subscription_id == subscription_uuid,
            BandwidthSample.sample_at >= start_at,
            BandwidthSample.sample_at <= end_at,
        )
        .first()
        or (0, 0)
    )

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

    avg_daily_usage_gb = (total_bytes / (1024 ** 3)) / total_days
    average_download_mbps = float(avg_rx_bps or 0) / 1_000_000
    average_upload_mbps = float(avg_tx_bps or 0) / 1_000_000
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
) -> dict:
    """Get usage page data for the customer portal."""
    subscription_id_str = _resolve_usage_subscription_id(db, customer)

    empty_result: dict[str, Any] = {
        "usage_records": [],
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
    }
    if not subscription_id_str:
        return empty_result

    subscription = db.get(Subscription, coerce_uuid(subscription_id_str))
    activated_at = None
    if subscription:
        activated_at = (
            _as_utc(getattr(subscription, "start_at", None))
            or _as_utc(getattr(subscription, "created_at", None))
        )
    start_at, end_at = _usage_period_bounds(period, activated_at=activated_at)

    usage_records, total = _daily_bandwidth_usage(
        db,
        subscription_id=subscription_id_str,
        start_at=start_at,
        end_at=end_at,
        page=page,
        per_page=per_page,
    )
    usage_summary = _usage_summary_stats(
        db,
        subscription_id=subscription_id_str,
        start_at=start_at,
        end_at=end_at,
    )

    return {
        "usage_records": usage_records,
        "period": period,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
        "usage_summary": usage_summary,
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

    services = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=account_id_str,
        offer_id=None,
        status=SubscriptionStatus.active.value,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = select(func.count(Subscription.id)).where(
        Subscription.subscriber_id == coerce_uuid(account_id_str),
        Subscription.status == SubscriptionStatus.active,
    )
    total = db.scalar(stmt) or 0

    return {
        "services": services,
        "status": SubscriptionStatus.active.value,
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

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "next_billing_date": next_billing_date,
    }


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
]
