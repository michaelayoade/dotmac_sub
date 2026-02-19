"""Service helpers for admin usage web pages."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import RadiusProfile, UsageAllowance
from app.models.domain_settings import SettingDomain
from app.models.usage import (
    UsageCharge,
    UsageChargeStatus,
    UsageRatingRun,
    UsageRatingRunStatus,
    UsageRecord,
)
from app.services import catalog as catalog_service
from app.services import settings_spec
from app.services import usage as usage_service
from app.services.common import coerce_uuid, validate_enum


def get_dashboard_data(db: Session) -> dict[str, object]:
    records = usage_service.usage_records.list(
        db=db,
        subscription_id=None,
        quota_bucket_id=None,
        order_by="recorded_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    staged_charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status="staged",
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    needs_review_charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status="needs_review",
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    posted_charges = usage_service.usage_charges.list(
        db=db,
        subscription_id=None,
        subscriber_id=None,
        status="posted",
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    recent_runs = usage_service.usage_rating_runs.list(
        db=db,
        status=None,
        order_by="run_at",
        order_dir="desc",
        limit=5,
        offset=0,
    )

    return {
        "records_count": len(records),
        "staged_count": len(staged_charges),
        "needs_review_count": len(needs_review_charges),
        "posted_count": len(posted_charges),
        "total_staged_amount": sum(float(charge.amount or 0) for charge in staged_charges),
        "recent_runs": recent_runs,
    }


def get_calculator_data(db: Session) -> dict[str, object]:
    usage_allowances = db.execute(
        select(UsageAllowance)
        .where(UsageAllowance.is_active.is_(True))
        .order_by(UsageAllowance.name)
    ).scalars().all()
    radius_profiles = db.execute(
        select(RadiusProfile)
        .where(RadiusProfile.is_active.is_(True))
        .order_by(RadiusProfile.name)
    ).scalars().all()
    currency_symbol = (
        settings_spec.resolve_value(db, SettingDomain.billing, "currency_symbol") or "₦"
    )
    return {
        "usage_allowances": usage_allowances,
        "radius_profiles": radius_profiles,
        "currency_symbol": currency_symbol,
    }


def get_records_page_data(
    db: Session,
    *,
    subscription_id: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    records_stmt = select(UsageRecord)
    if subscription_id:
        records_stmt = records_stmt.where(
            UsageRecord.subscription_id == coerce_uuid(subscription_id)
        )
    total = db.scalar(select(func.count()).select_from(records_stmt.subquery())) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    records = db.execute(
        records_stmt.order_by(UsageRecord.recorded_at.desc())
        .offset(offset)
        .limit(per_page)
    ).scalars().all()
    subscriptions = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=None,
        offer_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )
    return {
        "records": records,
        "subscriptions": subscriptions,
        "subscription_id": subscription_id,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def get_charges_page_data(
    db: Session,
    *,
    status: str | None,
    subscription_id: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    charges_stmt = select(UsageCharge)
    if subscription_id:
        charges_stmt = charges_stmt.where(
            UsageCharge.subscription_id == coerce_uuid(subscription_id)
        )
    if status:
        charges_stmt = charges_stmt.where(
            UsageCharge.status == validate_enum(status, UsageChargeStatus, "status")
        )
    total = db.scalar(select(func.count()).select_from(charges_stmt.subquery())) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    charges = db.execute(
        charges_stmt.order_by(UsageCharge.created_at.desc())
        .offset(offset)
        .limit(per_page)
    ).scalars().all()
    return {
        "charges": charges,
        "status": status,
        "subscription_id": subscription_id,
        "charge_statuses": [item.value for item in UsageChargeStatus],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def get_rating_runs_page_data(
    db: Session,
    *,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    runs_stmt = select(UsageRatingRun)
    if status:
        runs_stmt = runs_stmt.where(
            UsageRatingRun.status == validate_enum(status, UsageRatingRunStatus, "status")
        )
    total = db.scalar(select(func.count()).select_from(runs_stmt.subquery())) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    runs = db.execute(
        runs_stmt.order_by(UsageRatingRun.run_at.desc())
        .offset(offset)
        .limit(per_page)
    ).scalars().all()

    now = datetime.now(UTC)
    default_period_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    if now.month == 12:
        default_period_end = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        default_period_end = datetime(now.year, now.month + 1, 1, tzinfo=UTC)

    return {
        "runs": runs,
        "status": status,
        "run_statuses": [item.value for item in UsageRatingRunStatus],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "default_period_start": default_period_start.strftime("%Y-%m-%d"),
        "default_period_end": default_period_end.strftime("%Y-%m-%d"),
    }
