"""Subscriber growth and churn read owner for the admin reports.

This module is the canonical subscriber-domain read owner for the growth,
churn, and status figures rendered by the admin /reports pages. The web
report layer (``app.services.web_reports`` / ``web_reports_extended``)
composes these reads and owns presentation only (labels, floats, chart
shaping). Aggregations were moved here verbatim from the web layer so the
displayed numbers do not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import String, and_, case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.models.subscriber import AccountStatus, Subscriber, SubscriberStatus
from app.services import subscriber as subscriber_service
from app.timezone import APP_TIMEZONE


@dataclass(frozen=True, slots=True)
class MonthlyChurnSeries:
    labels: tuple[str, ...]
    rates: tuple[float, ...]
    counts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ChurnSummary:
    total: int
    cancelled_count: int
    suspended_count: int
    active_count: int = 0
    churn_count: int = 0


@dataclass(frozen=True, slots=True)
class RecentChurnEvent:
    """A subscriber-facing projection of one canonical churn event."""

    subscriber: Subscriber
    status: str
    occurred_at: datetime
    reason: str | None = None

    @property
    def id(self):
        """Keep the historical report-row contract for lightweight callers."""

        return self.subscriber.id


CHURN_STATUS_VALUES = (AccountStatus.canceled.value, AccountStatus.suspended.value)
TRUSTED_LIFECYCLE_EVIDENCE_GRADE = "transition_evidence"
TRUSTED_LIFECYCLE_EVIDENCE_SOURCE = "lifecycle_command"


def trusted_lifecycle_transition_clause():
    """Return the fail-closed admission rule for reportable transitions."""

    return and_(
        SubscriptionLifecycleEvent.evidence_grade == TRUSTED_LIFECYCLE_EVIDENCE_GRADE,
        SubscriptionLifecycleEvent.evidence_source == TRUSTED_LIFECYCLE_EVIDENCE_SOURCE,
        SubscriptionLifecycleEvent.source_id.is_not(None),
        SubscriptionLifecycleEvent.evidence_fingerprint.is_not(None),
        SubscriptionLifecycleEvent.effective_at.is_not(None),
        SubscriptionLifecycleEvent.recorded_at.is_not(None),
    )


def normalize_churn_status(status: str | None) -> str | None:
    """Return the supported churn-event filter or ``None`` for all events."""

    value = (status or "").strip().lower()
    if not value:
        return None
    if value not in CHURN_STATUS_VALUES:
        raise ValueError(f"Unsupported churn status: {status}")
    return value


def _churn_window(
    *, date_from: str | None = None, date_to: str | None = None
) -> tuple[datetime | None, datetime | None]:
    def local_midnight(value: str | None) -> datetime | None:
        text = (value or "").strip()
        if not text:
            return None
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime.combine(parsed, time.min, APP_TIMEZONE).astimezone(UTC)

    start = local_midnight(date_from)
    parsed_to = local_midnight(date_to)
    end = parsed_to + timedelta(days=1) if parsed_to else None
    return start, end


def churn_window(
    *, date_from: str | None = None, date_to: str | None = None
) -> tuple[datetime | None, datetime | None]:
    """Resolve report calendar dates in the application display timezone."""

    return _churn_window(date_from=date_from, date_to=date_to)


def _month_start(value: datetime) -> datetime:
    local = value.astimezone(APP_TIMEZONE)
    return datetime(local.year, local.month, 1, tzinfo=APP_TIMEZONE).astimezone(UTC)


def _next_month(value: datetime) -> datetime:
    local = value.astimezone(APP_TIMEZONE)
    if local.month == 12:
        next_year, next_month = local.year + 1, 1
    else:
        next_year, next_month = local.year, local.month + 1
    return datetime(next_year, next_month, 1, tzinfo=APP_TIMEZONE).astimezone(UTC)


def _event_status_clause(status: str | None):
    normalized = normalize_churn_status(status)
    if normalized == AccountStatus.canceled.value:
        return Subscriber.status == AccountStatus.canceled
    if normalized == AccountStatus.suspended.value:
        return Subscriber.status == AccountStatus.suspended
    return or_(
        Subscriber.status == AccountStatus.canceled,
        Subscriber.status == AccountStatus.suspended,
    )


def _churn_event_source(
    *,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Return one normalized churn-event relation for all report projections.

    Trusted lifecycle evidence is authoritative. The legacy branch keeps rows
    created before lifecycle evidence was available visible until they can be
    backfilled, but it is disabled for subscribers that already have trusted
    churn evidence so a transition is never counted twice.
    """
    normalized_status = normalize_churn_status(status)
    start, end = _churn_window(date_from=date_from, date_to=date_to)
    lifecycle_status = case(
        (
            SubscriptionLifecycleEvent.event_type == LifecycleEventType.cancel,
            literal(AccountStatus.canceled.value),
        ),
        else_=literal(AccountStatus.suspended.value),
    ).label("status")
    lifecycle = (
        select(
            Subscription.subscriber_id.label("subscriber_id"),
            lifecycle_status,
            SubscriptionLifecycleEvent.effective_at.label("occurred_at"),
            SubscriptionLifecycleEvent.reason.label("reason"),
        )
        .select_from(SubscriptionLifecycleEvent)
        .join(
            Subscription,
            Subscription.id == SubscriptionLifecycleEvent.subscription_id,
        )
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .where(
            subscriber_service.visible_subscriber_clause(),
            SubscriptionLifecycleEvent.event_type.in_(
                (LifecycleEventType.cancel, LifecycleEventType.suspend)
            ),
            trusted_lifecycle_transition_clause(),
        )
    )
    if normalized_status == AccountStatus.canceled.value:
        lifecycle = lifecycle.where(
            SubscriptionLifecycleEvent.event_type == LifecycleEventType.cancel
        )
    elif normalized_status == AccountStatus.suspended.value:
        lifecycle = lifecycle.where(
            SubscriptionLifecycleEvent.event_type == LifecycleEventType.suspend
        )

    trusted_churn_exists = (
        select(1)
        .select_from(SubscriptionLifecycleEvent)
        .join(
            Subscription,
            Subscription.id == SubscriptionLifecycleEvent.subscription_id,
        )
        .where(
            Subscription.subscriber_id == Subscriber.id,
            SubscriptionLifecycleEvent.event_type.in_(
                (LifecycleEventType.cancel, LifecycleEventType.suspend)
            ),
            trusted_lifecycle_transition_clause(),
        )
        .exists()
    )
    legacy_status = case(
        (
            Subscriber.status == AccountStatus.canceled,
            literal(AccountStatus.canceled.value),
        ),
        else_=literal(AccountStatus.suspended.value),
    ).label("status")
    legacy = select(
        Subscriber.id.label("subscriber_id"),
        legacy_status,
        func.coalesce(Subscriber.updated_at, Subscriber.created_at).label(
            "occurred_at"
        ),
        literal(None, type_=String()).label("reason"),
    ).where(
        subscriber_service.visible_subscriber_clause(),
        Subscriber.status.in_((AccountStatus.canceled, AccountStatus.suspended)),
        ~trusted_churn_exists,
    )
    if normalized_status == AccountStatus.canceled.value:
        legacy = legacy.where(Subscriber.status == AccountStatus.canceled)
    elif normalized_status == AccountStatus.suspended.value:
        legacy = legacy.where(Subscriber.status == AccountStatus.suspended)

    source = union_all(lifecycle, legacy).subquery("churn_events")
    if start is not None:
        source = select(source).where(source.c.occurred_at >= start).subquery()
    if end is not None:
        source = select(source).where(source.c.occurred_at < end).subquery()
    return source


def _month_starts(months: int = 6) -> list[datetime]:
    starts: list[datetime] = []
    cursor = _month_start(datetime.now(UTC))
    for _ in range(months):
        starts.append(cursor)
        local = cursor.astimezone(APP_TIMEZONE)
        if local.month == 1:
            previous_year, previous_month = local.year - 1, 12
        else:
            previous_year, previous_month = local.year, local.month - 1
        cursor = datetime(
            previous_year, previous_month, 1, tzinfo=APP_TIMEZONE
        ).astimezone(UTC)
    starts.reverse()
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


def monthly_churn_series(
    db: Session,
    *,
    months: int = 6,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    population_total: int | None = None,
) -> MonthlyChurnSeries:
    """Monthly churn-event counts for the requested status/date window.

    With no explicit dates this retains the report's trailing six calendar
    months. A custom range clips the first and last bucket, so a range such as
    ``2026-06-28`` through ``2026-08-28`` cannot leak events from June 1–27
    or August 29 onward.
    """
    now = datetime.now(UTC)
    parsed_start, parsed_end = _churn_window(date_from=date_from, date_to=date_to)
    if date_from or date_to:
        window_end = parsed_end or now
        window_start = parsed_start or _month_start(
            window_end - timedelta(days=months * 31)
        )
        starts: list[datetime] = []
        cursor = _month_start(window_start)
        while cursor < window_end:
            starts.append(cursor)
            cursor = _next_month(cursor)
    else:
        starts = _month_starts(months)
        window_start = starts[0]
        window_end = now

    bucket_ranges: list[tuple[datetime, datetime]] = []
    labels: list[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else window_end
        bucket_start = max(start, window_start)
        bucket_end = min(end, window_end)
        if bucket_start >= bucket_end:
            continue
        bucket_ranges.append((bucket_start, bucket_end))
        labels.append(
            start.strftime("%b %Y") if date_from or date_to else start.strftime("%b")
        )

    counts_by_bucket: dict[int, int] = {}
    if bucket_ranges:
        source = _churn_event_source(
            status=status, date_from=date_from, date_to=date_to
        )
        bucket_case = case(
            *[
                (
                    and_(
                        source.c.occurred_at >= bucket_start,
                        source.c.occurred_at < bucket_end,
                    ),
                    index,
                )
                for index, (bucket_start, bucket_end) in enumerate(bucket_ranges)
            ],
            else_=None,
        ).label("bucket")
        distinct_events = (
            select(source.c.subscriber_id, bucket_case)
            .where(
                source.c.occurred_at >= window_start,
                source.c.occurred_at < window_end,
            )
            .distinct()
            .subquery()
        )
        counts_by_bucket = {
            int(bucket): int(count)
            for bucket, count in db.execute(
                select(distinct_events.c.bucket, func.count())
                .where(distinct_events.c.bucket.is_not(None))
                .group_by(distinct_events.c.bucket)
            ).all()
        }

    if population_total is None:
        population_statement = select(func.count(Subscriber.id)).where(
            subscriber_service.visible_subscriber_clause()
        )
        if window_end < now:
            population_statement = population_statement.where(
                Subscriber.created_at < window_end
            )
        population_total = int(db.scalar(population_statement) or 0)

    counts = [counts_by_bucket.get(index, 0) for index in range(len(bucket_ranges))]
    rates = [
        round((count / population_total * 100) if population_total else 0, 1)
        for count in counts
    ]
    return MonthlyChurnSeries(
        labels=tuple(labels), rates=tuple(rates), counts=tuple(counts)
    )


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


def _population_counts(db: Session, *, end: datetime | None) -> tuple[int, int]:
    """Return cohort size and active accounts as of the selected period end."""
    population_filters = [subscriber_service.visible_subscriber_clause()]
    if end is not None:
        population_filters.append(Subscriber.created_at < end)

    if end is None:
        statement = select(
            func.count(Subscriber.id),
            func.coalesce(
                func.sum(case((Subscriber.status == AccountStatus.active, 1), else_=0)),
                0,
            ),
        ).where(*population_filters)
        total, active = db.execute(statement).one()
        return int(total or 0), int(active or 0)

    event_rows = (
        select(
            Subscription.subscriber_id.label("subscriber_id"),
            SubscriptionLifecycleEvent.effective_at.label("effective_at"),
            SubscriptionLifecycleEvent.from_status.label("from_status"),
            SubscriptionLifecycleEvent.to_status.label("to_status"),
        )
        .select_from(SubscriptionLifecycleEvent)
        .join(
            Subscription,
            Subscription.id == SubscriptionLifecycleEvent.subscription_id,
        )
        .where(trusted_lifecycle_transition_clause())
        .subquery("population_lifecycle_events")
    )
    before_ranked = (
        select(
            event_rows,
            func.row_number()
            .over(
                partition_by=event_rows.c.subscriber_id,
                order_by=event_rows.c.effective_at.desc(),
            )
            .label("event_rank"),
        )
        .where(event_rows.c.effective_at <= end)
        .subquery("population_before_ranked")
    )
    before = (
        select(before_ranked)
        .where(before_ranked.c.event_rank == 1)
        .subquery("population_before")
    )
    after_ranked = (
        select(
            event_rows,
            func.row_number()
            .over(
                partition_by=event_rows.c.subscriber_id,
                order_by=event_rows.c.effective_at.asc(),
            )
            .label("event_rank"),
        )
        .where(event_rows.c.effective_at > end)
        .subquery("population_after_ranked")
    )
    after = (
        select(after_ranked)
        .where(after_ranked.c.event_rank == 1)
        .subquery("population_after")
    )
    active_at_end = case(
        (
            before.c.subscriber_id.is_not(None),
            case((before.c.to_status == SubscriptionStatus.active, 1), else_=0),
        ),
        (
            and_(
                after.c.subscriber_id.is_not(None),
                after.c.from_status.is_not(None),
            ),
            case((after.c.from_status == SubscriptionStatus.active, 1), else_=0),
        ),
        else_=case((Subscriber.status == AccountStatus.active, 1), else_=0),
    ).label("active_at_end")
    population = (
        select(Subscriber.id, active_at_end)
        .select_from(Subscriber)
        .outerjoin(before, before.c.subscriber_id == Subscriber.id)
        .outerjoin(after, after.c.subscriber_id == Subscriber.id)
        .where(*population_filters)
        .subquery("churn_population")
    )
    total, active = db.execute(
        select(
            func.count(population.c.id),
            func.coalesce(func.sum(population.c.active_at_end), 0),
        )
    ).one()
    return int(total or 0), int(active or 0)


def churn_summary(
    db: Session,
    *,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ChurnSummary:
    """Cancelled / suspended / total counts over admin-visible subscribers.

    Counts trusted lifecycle cancel/suspend events and falls back to current
    subscriber state only for subscribers without trusted churn evidence. The
    population is the visible subscriber base created by the period end.
    """
    normalized_status = normalize_churn_status(status)
    _start, end = _churn_window(date_from=date_from, date_to=date_to)
    total, active_count = _population_counts(db, end=end)

    source = _churn_event_source(
        status=normalized_status, date_from=date_from, date_to=date_to
    )
    unique_events = (
        select(source.c.subscriber_id, source.c.status).distinct().subquery()
    )
    event_counts = db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (unique_events.c.status == AccountStatus.canceled.value, 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (unique_events.c.status == AccountStatus.suspended.value, 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.count(func.distinct(unique_events.c.subscriber_id)),
        ).select_from(unique_events)
    ).one()
    cancelled, suspended, churn_count = (int(value or 0) for value in event_counts)
    return ChurnSummary(
        total=int(total),
        cancelled_count=int(cancelled),
        suspended_count=int(suspended),
        active_count=int(active_count or 0),
        churn_count=int(churn_count or 0),
    )


def recent_churn_events(
    db: Session,
    *,
    limit: int | None = 10,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[RecentChurnEvent]:
    """Most recent cancellation/suspension events in the selected window.

    Loads at most ``limit`` unique subscribers from the normalized event
    relation. Ordering and limiting happen in SQL so large event histories do
    not block the report page.
    """
    source = _churn_event_source(status=status, date_from=date_from, date_to=date_to)
    ranked = select(
        source,
        func.row_number()
        .over(
            partition_by=source.c.subscriber_id,
            order_by=source.c.occurred_at.desc(),
        )
        .label("event_rank"),
    ).subquery()
    statement = (
        select(
            Subscriber,
            ranked.c.status,
            ranked.c.occurred_at,
            ranked.c.reason,
        )
        .join(ranked, ranked.c.subscriber_id == Subscriber.id)
        .where(ranked.c.event_rank == 1)
        .order_by(ranked.c.occurred_at.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    return [
        RecentChurnEvent(
            subscriber=subscriber,
            status=status_value,
            occurred_at=occurred_at,
            reason=reason,
        )
        for subscriber, status_value, occurred_at, reason in db.execute(statement).all()
    ]


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
