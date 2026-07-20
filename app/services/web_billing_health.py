"""View-model helpers for the admin billing health page."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.autopay import AutopayMandate
from app.models.scheduler import ScheduledTask
from app.models.subscriber import Subscriber
from app.services import autopay, billing_health, billing_integrity_audit
from app.services.job_heartbeat import get_last_success

AUTOPAY_TASK_NAME = "app.tasks.autopay.charge_due_invoices"


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (now - value).total_seconds()


def _autopay_task_status(db: Session, now: datetime) -> dict[str, Any]:
    row = db.execute(
        select(
            ScheduledTask.name,
            ScheduledTask.enabled,
            ScheduledTask.interval_seconds,
        )
        .where(ScheduledTask.task_name == AUTOPAY_TASK_NAME)
        .limit(1)
    ).first()
    last_success = get_last_success(AUTOPAY_TASK_NAME)
    values = row._mapping if row else {}
    interval = (
        int(values["interval_seconds"])
        if values and values["interval_seconds"]
        else None
    )
    age = _age_seconds(last_success, now)
    return {
        "name": values["name"] if values else "autopay_runner",
        "task_name": AUTOPAY_TASK_NAME,
        "enabled": bool(values["enabled"]) if values else False,
        "interval_seconds": interval,
        "last_success": last_success,
        "age_seconds": age,
        "stale": bool(values and values["enabled"])
        and (
            last_success is None
            or (interval is not None and age is not None and age > interval * 3)
        ),
    }


def _build_autopay_summary(db: Session, now: datetime) -> dict[str, Any]:
    failure_cap = autopay.max_consecutive_failures(db)
    totals = dict(
        total=int(db.execute(select(func.count(AutopayMandate.id))).scalar() or 0),
        active=int(
            db.execute(
                select(func.count(AutopayMandate.id)).where(
                    AutopayMandate.is_active.is_(True)
                )
            ).scalar()
            or 0
        ),
        inactive=int(
            db.execute(
                select(func.count(AutopayMandate.id)).where(
                    AutopayMandate.is_active.is_(False)
                )
            ).scalar()
            or 0
        ),
        with_failures=int(
            db.execute(
                select(func.count(AutopayMandate.id)).where(
                    AutopayMandate.failure_count > 0
                )
            ).scalar()
            or 0
        ),
        suspended=int(
            db.execute(
                select(func.count(AutopayMandate.id))
                .where(AutopayMandate.is_active.is_(True))
                .where(AutopayMandate.failure_count >= failure_cap)
            ).scalar()
            or 0
        ),
    )
    recent_failures = [
        {
            "account_id": str(mandate.account_id),
            "account_name": (
                f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
                if subscriber
                else "Account"
            ),
            "failure_count": int(mandate.failure_count or 0),
            "last_failure_at": mandate.last_failure_at,
            "last_failure_reason": mandate.last_failure_reason or "",
            "suspended": bool(
                mandate.is_active and int(mandate.failure_count or 0) >= failure_cap
            ),
        }
        for mandate, subscriber in db.execute(
            select(AutopayMandate, Subscriber)
            .outerjoin(Subscriber, Subscriber.id == AutopayMandate.account_id)
            .where(AutopayMandate.failure_count > 0)
            .order_by(desc(AutopayMandate.last_failure_at))
            .limit(10)
        ).all()
    ]
    return {
        **totals,
        "failure_cap": failure_cap,
        "task": _autopay_task_status(db, now),
        "recent_failures": recent_failures,
    }


def build_billing_health_data(
    db: Session, now: datetime | None = None
) -> dict[str, Any]:
    """Build a read-only health/integrity/autopay snapshot for admins."""
    now = now or datetime.now(UTC)
    snapshot = billing_health.billing_health_snapshot(db, now=now)
    integrity = billing_integrity_audit.audit_billing_integrity(db)
    counts = integrity.get("counts", {})
    blocking_count = sum(
        int(counts.get(name, 0) or 0)
        for name in (
            "billing_disabled_service_lines",
            "billing_duplicate_subscription_period_lines",
            "billing_addon_without_billable_parent",
            "active_subscription_missing_radius",
        )
    )
    warning_count = sum(
        int(value or 0)
        for name, value in counts.items()
        if name
        not in {
            "billing_disabled_service_lines",
            "billing_duplicate_subscription_period_lines",
            "billing_addon_without_billable_parent",
            "active_subscription_missing_radius",
        }
    )
    return {
        "snapshot": snapshot,
        "integrity": integrity,
        "integrity_blocking_count": blocking_count,
        "integrity_warning_count": warning_count,
        "autopay": _build_autopay_summary(db, now),
        "generated_at": now,
    }
