"""Subscriber growth and churn read owner for the admin reports.

This module is the canonical subscriber-domain read owner for the growth,
churn, and status figures rendered by the admin /reports pages. The web
report layer (``app.services.web_reports`` / ``web_reports_extended``)
composes these reads and owns presentation only (labels, floats, chart
shaping). Aggregations were moved here verbatim from the web layer so the
displayed numbers do not change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.subscriber import AccountStatus, Subscriber, SubscriberStatus
from app.services import subscriber as subscriber_service


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


def monthly_customer_growth_series(db: Session, *, months: int = 6) -> dict[str, list]:
    """Monthly running total and new-signup counts for visible subscribers."""
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


def monthly_churn_series(db: Session, *, months: int = 6) -> dict[str, list]:
    """Monthly cancellation counts and churn rates for visible subscribers."""
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


def monthly_new_counts(db: Session) -> tuple[int, int]:
    """(current-month, previous-month) new visible-subscriber counts.

    The web layer computes the growth percent from these; the count
    definitions live here.
    """
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
    return int(current_new), int(previous_new)


def _derived_cancelled_clause():
    """SQL form of the report's derived-status rule for "cancelled".

    A subscriber with an explicit status keeps it; a NULL status derives to
    ``active`` when ``is_active`` is truthy and ``canceled`` otherwise.
    """
    return or_(
        Subscriber.status == AccountStatus.canceled,
        and_(Subscriber.status.is_(None), Subscriber.is_active.is_not(True)),
    )


def churn_summary(db: Session) -> dict:
    """Cancelled / at-risk / total counts over admin-visible subscribers.

    Replicates in SQL the counts the churn report previously computed by
    loading every visible subscriber and deriving its status in Python:
    ``cancelled`` uses the derived-status rule (an explicit ``canceled``
    status, or a NULL status with a falsy ``is_active``); ``at_risk`` is an
    explicit ``suspended`` status (a NULL status can never derive to
    suspended).
    """
    total = (
        db.scalar(
            select(func.count(Subscriber.id)).where(
                subscriber_service.visible_subscriber_clause()
            )
        )
        or 0
    )
    cancelled = (
        db.scalar(
            select(func.count(Subscriber.id)).where(
                subscriber_service.visible_subscriber_clause(),
                _derived_cancelled_clause(),
            )
        )
        or 0
    )
    at_risk = (
        db.scalar(
            select(func.count(Subscriber.id)).where(
                subscriber_service.visible_subscriber_clause(),
                Subscriber.status == AccountStatus.suspended,
            )
        )
        or 0
    )
    return {
        "total": int(total),
        "cancelled_count": int(cancelled),
        "at_risk_count": int(at_risk),
    }


def recent_cancellations(db: Session, *, limit: int = 10) -> list[Subscriber]:
    """Most recently cancelled admin-visible subscribers.

    Loads only the derived-cancelled rows (ordered ``created_at`` desc, the
    same base order the report's full-table load used, so ties sort the same
    way) and sorts by the effective updated-at in Python because that value
    can come from imported metadata.
    """
    cancelled = list(
        db.scalars(
            select(Subscriber)
            .where(
                subscriber_service.visible_subscriber_clause(),
                _derived_cancelled_clause(),
            )
            .order_by(Subscriber.created_at.desc())
        ).all()
    )
    for sub in cancelled:
        if sub.status is None:
            sub.status = AccountStatus.canceled
    cancelled.sort(
        key=lambda x: (
            subscriber_service.get_effective_updated_at(x)
            or datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True,
    )
    return cancelled[:limit]


def status_counts(db: Session) -> dict[str, int]:
    """Admin-visible subscriber count per explicit status value.

    Rows with a NULL status fall into no bucket, matching the Python
    counting the growth report previously did over loaded rows.
    """
    rows = db.execute(
        select(Subscriber.status, func.count(Subscriber.id))
        .where(subscriber_service.visible_subscriber_clause())
        .group_by(Subscriber.status)
    ).all()
    by_status = {row[0]: int(row[1] or 0) for row in rows}
    return {s.value: by_status.get(s, 0) for s in SubscriberStatus}


def daily_cumulative_signups(db: Session, *, days: int = 30) -> dict:
    """Cumulative daily signup series over the last ``days`` days.

    Returns ``{"total", "new_this_month", "labels", "data"}``. Loads the
    lightweight admin-visible subscriber rows and computes the series in
    Python (moved verbatim from the web layer) because the effective signup
    date can come from imported metadata
    (``subscriber_service.get_effective_created_at``), which has no exact SQL
    equivalent.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=days)

    visible_subscribers: list[Any] = [
        SimpleNamespace(
            metadata_=row.metadata_,
            splynx_customer_id=row.splynx_customer_id,
            account_start_date=row.account_start_date,
            created_at=row.created_at,
        )
        for row in db.execute(
            select(
                Subscriber.metadata_,
                Subscriber.splynx_customer_id,
                Subscriber.account_start_date,
                Subscriber.created_at,
            ).where(subscriber_service.visible_subscriber_clause())
        ).all()
    ]
    total = len(visible_subscribers)

    # New this month
    month_start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_this_month = 0
    for row in visible_subscribers:
        created_at = subscriber_service.get_effective_created_at(row)
        if created_at is not None and created_at >= month_start:
            new_this_month += 1

    # Daily chart data — cumulative subscriber count per day
    chart_labels = []
    chart_data = []
    for i in range(days):
        day = start + timedelta(days=i)
        chart_labels.append(day.strftime("%Y-%m-%d"))
        day_count = 0
        for row in visible_subscribers:
            created_at = subscriber_service.get_effective_created_at(row)
            if created_at is not None and created_at <= day:
                day_count += 1
        chart_data.append(day_count)

    return {
        "total": total,
        "new_this_month": new_this_month,
        "labels": chart_labels,
        "data": chart_data,
    }
