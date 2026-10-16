"""Ticket SLA reporting helpers."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, distinct, func
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.support import Ticket, TicketStatus
from app.models.system_user import SystemUser
from app.models.ticket_workflow import (
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    WorkflowEntityType,
)


@dataclass(frozen=True, slots=True)
class TicketSlaExportQuery:
    start_at: datetime | None = None
    end_at: datetime | None = None
    open_only: bool = False


@dataclass(frozen=True, slots=True)
class TicketSlaViolationPageQuery:
    start_at: datetime | None = None
    end_at: datetime | None = None
    open_only: bool = False
    page: int = 1
    per_page: int = 15


@dataclass(frozen=True, slots=True)
class TicketSlaViolationRecord:
    ticket_id: str
    ticket_reference: str
    ticket_url: str
    title: str
    status: str
    priority: str
    region: str
    service_team_id: str | None
    service_team: str
    assignee_person_id: str | None
    assignee: str
    sla_status: str
    started_at: datetime
    due_at: datetime
    breached_at: datetime
    breach_minutes: int
    breach_duration: str


@dataclass(frozen=True, slots=True)
class TicketSlaViolationPage:
    rows: tuple[TicketSlaViolationRecord, ...]
    page: int
    per_page: int
    total_count: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


@dataclass(frozen=True, slots=True)
class TicketSlaSummaryQuery:
    """Current operational ticket scope, optionally bounded by creation time."""

    start_at: datetime | None = None
    end_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TicketSlaMetricBucket:
    key: str
    label: str
    open_tickets: int
    currently_breaching: int
    breach_rate: float


@dataclass(frozen=True, slots=True)
class TicketSlaSummary:
    generated_at: datetime
    total_open_tickets: int
    total_currently_breaching: int
    current_breach_rate: float
    by_status: tuple[TicketSlaMetricBucket, ...]
    by_service_team: tuple[TicketSlaMetricBucket, ...]
    by_region: tuple[TicketSlaMetricBucket, ...]
    by_assignee: tuple[TicketSlaMetricBucket, ...]

    def as_serializable(self) -> dict[str, object]:
        def bucket_values(
            buckets: tuple[TicketSlaMetricBucket, ...],
        ) -> list[dict[str, object]]:
            return [
                {
                    "key": bucket.key,
                    "label": bucket.label,
                    "open_tickets": bucket.open_tickets,
                    "currently_breaching": bucket.currently_breaching,
                    "breach_rate": bucket.breach_rate,
                }
                for bucket in buckets
            ]

        return {
            "generated_at": self.generated_at.isoformat(),
            "total_open_tickets": self.total_open_tickets,
            "total_currently_breaching": self.total_currently_breaching,
            "current_breach_rate": self.current_breach_rate,
            "by_status": bucket_values(self.by_status),
            "by_service_team": bucket_values(self.by_service_team),
            "by_region": bucket_values(self.by_region),
            "by_assignee": bucket_values(self.by_assignee),
        }


@dataclass(frozen=True, slots=True)
class TicketPerformancePoint:
    """One calendar-day point in the dashboard ticket-performance chart."""

    day: date
    newly_created: int
    approaching_sla: int
    breached_sla: int


@dataclass(frozen=True, slots=True)
class TicketPerformanceSeries:
    """One service-team series, with an explicit all-teams cohort."""

    key: str
    label: str
    points: tuple[TicketPerformancePoint, ...]


@dataclass(frozen=True, slots=True)
class TicketPerformanceDashboard:
    """Typed, freshness-bearing dashboard projection for ticket operations."""

    observed_at: datetime
    window_days: int
    series: tuple[TicketPerformanceSeries, ...]

    def as_serializable(self) -> list[dict[str, object]]:
        return [
            {
                "key": item.key,
                "label": item.label,
                "points": [
                    {
                        "label": point.day.strftime("%a"),
                        "date": point.day.isoformat(),
                        "newly_created": point.newly_created,
                        "approaching_sla": point.approaching_sla,
                        "breached_sla": point.breached_sla,
                    }
                    for point in item.points
                ],
            }
            for item in self.series
        ]


def dashboard_performance(
    db: Session, *, observed_at: datetime | None = None, window_days: int = 7
) -> TicketPerformanceDashboard:
    """Project real ticket activity for the dashboard, grouped by service team.

    Historical points use canonical ticket creation and SLA breach timestamps.
    "Approaching SLA" is a current-state signal, so it appears only on the
    observation day and uses each clock's configured warning window.
    """

    from app.models.ticket_workflow import SlaTarget

    now = _as_aware_utc(observed_at) or datetime.now(UTC)
    days = max(2, min(int(window_days), 30))
    first_day = now.date() - timedelta(days=days - 1)
    start_at = datetime.combine(first_day, datetime.min.time(), tzinfo=UTC)
    day_values = tuple(first_day + timedelta(days=index) for index in range(days))

    active_teams = (
        db.query(ServiceTeam.id, ServiceTeam.name)
        .filter(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.name.asc())
        .all()
    )
    team_labels = {str(team_id): str(name) for team_id, name in active_teams}
    team_labels["unassigned"] = "Unassigned"

    created_rows = (
        db.query(
            func.date(Ticket.created_at), Ticket.service_team_id, func.count(Ticket.id)
        )
        .filter(Ticket.is_active.is_(True), Ticket.created_at >= start_at)
        .group_by(func.date(Ticket.created_at), Ticket.service_team_id)
        .all()
    )
    breached_rows = (
        db.query(
            func.date(SlaClock.breached_at),
            Ticket.service_team_id,
            func.count(distinct(Ticket.id)),
        )
        .join(
            Ticket,
            (Ticket.id == SlaClock.entity_id)
            & (SlaClock.entity_type == WorkflowEntityType.ticket.value),
        )
        .filter(Ticket.is_active.is_(True), SlaClock.breached_at >= start_at)
        .group_by(func.date(SlaClock.breached_at), Ticket.service_team_id)
        .all()
    )

    warning_rows = (
        db.query(SlaTarget.policy_id, SlaTarget.priority, SlaTarget.warning_minutes)
        .filter(SlaTarget.is_active.is_(True))
        .all()
    )
    warning_by_target = {
        (row.policy_id, str(row.priority or "").strip().lower()): int(
            row.warning_minutes or 0
        )
        for row in warning_rows
    }
    clock_rows = (
        db.query(
            Ticket.id,
            Ticket.service_team_id,
            SlaClock.policy_id,
            SlaClock.priority,
            SlaClock.due_at,
        )
        .join(
            Ticket,
            (Ticket.id == SlaClock.entity_id)
            & (SlaClock.entity_type == WorkflowEntityType.ticket.value),
        )
        .filter(
            Ticket.is_active.is_(True),
            Ticket.status.in_(
                (
                    TicketStatus.new.value,
                    TicketStatus.open.value,
                    TicketStatus.pending.value,
                    TicketStatus.lastmile_rerun.value,
                    TicketStatus.waiting_on_customer.value,
                    TicketStatus.on_hold.value,
                    TicketStatus.site_under_construction.value,
                )
            ),
            SlaClock.status == SlaClockStatus.running.value,
        )
        .all()
    )

    def team_key(raw: object) -> str:
        return str(raw) if raw is not None else "unassigned"

    def calendar_day(raw: object) -> date:
        return raw if isinstance(raw, date) else date.fromisoformat(str(raw))

    created: dict[tuple[date, str], int] = {}
    breached: dict[tuple[date, str], int] = {}
    approaching: dict[str, int] = {}
    approaching_ticket_ids: set[UUID] = set()
    for day_value, service_team_id, count in created_rows:
        created[(calendar_day(day_value), team_key(service_team_id))] = int(count or 0)
    for day_value, service_team_id, count in breached_rows:
        if day_value is not None:
            breached[(calendar_day(day_value), team_key(service_team_id))] = int(
                count or 0
            )
    for row in clock_rows:
        due_at = _as_aware_utc(row.due_at)
        warning_minutes = warning_by_target.get(
            (row.policy_id, str(row.priority or "").strip().lower()),
            warning_by_target.get((row.policy_id, ""), 0),
        )
        if (
            due_at is not None
            and warning_minutes > 0
            and now < due_at <= now + timedelta(minutes=warning_minutes)
            and row.id not in approaching_ticket_ids
        ):
            approaching_ticket_ids.add(row.id)
            key = team_key(row.service_team_id)
            approaching[key] = approaching.get(key, 0) + 1

    team_keys = sorted(
        set(team_labels) | {key for _, key in created} | {key for _, key in breached},
        key=lambda key: (team_labels.get(key, "Unassigned").lower(), key),
    )

    def points_for(key: str | None) -> tuple[TicketPerformancePoint, ...]:
        return tuple(
            TicketPerformancePoint(
                day=day_value,
                newly_created=sum(
                    count
                    for (point_day, point_key), count in created.items()
                    if point_day == day_value and (key is None or point_key == key)
                ),
                approaching_sla=(
                    sum(approaching.values())
                    if key is None and day_value == now.date()
                    else approaching.get(key, 0)
                    if key is not None and day_value == now.date()
                    else 0
                ),
                breached_sla=sum(
                    count
                    for (point_day, point_key), count in breached.items()
                    if point_day == day_value and (key is None or point_key == key)
                ),
            )
            for day_value in day_values
        )

    series = [
        TicketPerformanceSeries(
            key="all", label="All service teams", points=points_for(None)
        )
    ]
    series.extend(
        TicketPerformanceSeries(
            key=key,
            label=team_labels.get(key, "Unassigned"),
            points=points_for(key),
        )
        for key in team_keys
    )
    return TicketPerformanceDashboard(
        observed_at=now,
        window_days=days,
        series=tuple(series),
    )


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _duration_minutes(started_at: datetime | None, ended_at: datetime | None) -> int:
    start_value = _as_aware_utc(started_at)
    if start_value is None:
        return 0
    end_value = _as_aware_utc(ended_at) or datetime.now(UTC)
    return max(int((end_value - start_value).total_seconds() // 60), 0)


def _duration_label(minutes: int) -> str:
    if minutes <= 0:
        return "0m"
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def _ticket_clock_query(db: Session):
    return (
        db.query(SlaClock)
        .join(Ticket, Ticket.id == SlaClock.entity_id)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(Ticket.is_active.is_(True))
    )


def _apply_clock_window(query, start_at: datetime | None, end_at: datetime | None):
    if start_at:
        query = query.filter(SlaClock.started_at >= start_at)
    if end_at:
        query = query.filter(SlaClock.started_at <= end_at)
    return query


def _apply_ticket_window(query, start_at: datetime | None, end_at: datetime | None):
    if start_at:
        query = query.filter(Ticket.created_at >= start_at)
    if end_at:
        query = query.filter(Ticket.created_at <= end_at)
    return query


def _current_ticket_buckets(
    rows: list[tuple[object, object, int, int]],
    *,
    none_key: str,
    none_label: str,
) -> tuple[TicketSlaMetricBucket, ...]:
    buckets: list[TicketSlaMetricBucket] = []
    for key, label, open_tickets, currently_breaching in rows:
        open_count = int(open_tickets or 0)
        breach_count = int(currently_breaching or 0)
        buckets.append(
            TicketSlaMetricBucket(
                key=str(getattr(key, "value", key) or none_key),
                label=str(getattr(label, "value", label) or none_label),
                open_tickets=open_count,
                currently_breaching=breach_count,
                breach_rate=(
                    round(breach_count / open_count, 4) if open_count else 0.0
                ),
            )
        )
    return tuple(
        sorted(
            buckets,
            key=lambda item: (
                -item.currently_breaching,
                -item.open_tickets,
                item.label.lower(),
            ),
        )
    )


def summary(db: Session, *, query: TicketSlaSummaryQuery) -> TicketSlaSummary:
    """Project currently breaching tickets over the current open workload."""

    excluded_statuses = (
        TicketStatus.closed.value,
        TicketStatus.canceled.value,
        "merged",
    )
    ticket_base = _apply_ticket_window(
        db.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.notin_(excluded_statuses)),
        query.start_at,
        query.end_at,
    )
    clock_join = (SlaClock.entity_id == Ticket.id) & (
        SlaClock.entity_type == WorkflowEntityType.ticket.value
    )
    current_breach_filter = SlaClock.status == SlaClockStatus.breached.value
    current_breach_ticket_id = case((current_breach_filter, Ticket.id), else_=None)
    by_status = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .with_entities(
            Ticket.status,
            Ticket.status,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.status)
        .all()
    )
    by_team = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .outerjoin(ServiceTeam, ServiceTeam.id == Ticket.service_team_id)
        .with_entities(
            Ticket.service_team_id,
            ServiceTeam.name,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.service_team_id, ServiceTeam.name)
        .all()
    )
    by_region = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .with_entities(
            Ticket.region,
            Ticket.region,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.region)
        .all()
    )
    total_open_tickets = int(ticket_base.count())
    total_currently_breaching = int(
        ticket_base.outerjoin(SlaClock, clock_join)
        .with_entities(func.count(distinct(Ticket.id)))
        .filter(current_breach_filter)
        .scalar()
        or 0
    )
    by_assignee = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .outerjoin(SystemUser, SystemUser.id == Ticket.assigned_to_person_id)
        .with_entities(
            Ticket.assigned_to_person_id,
            SystemUser.display_name,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.assigned_to_person_id, SystemUser.display_name)
        .all()
    )
    return TicketSlaSummary(
        generated_at=datetime.now(UTC),
        total_open_tickets=total_open_tickets,
        total_currently_breaching=total_currently_breaching,
        current_breach_rate=(
            round(total_currently_breaching / total_open_tickets, 4)
            if total_open_tickets
            else 0.0
        ),
        by_status=_current_ticket_buckets(
            by_status, none_key="unknown_status", none_label="Unknown Status"
        ),
        by_service_team=_current_ticket_buckets(
            by_team, none_key="unassigned_team", none_label="Unassigned Team"
        ),
        by_region=_current_ticket_buckets(
            by_region,
            none_key="unassigned_region",
            none_label="Unassigned Region",
        ),
        by_assignee=_current_ticket_buckets(
            by_assignee, none_key="unassigned_person", none_label="Unassigned Person"
        ),
    )


def trend_daily(
    db: Session, start_at: datetime | None = None, end_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Group ticket SLA clocks by start day."""
    base = _apply_clock_window(_ticket_clock_query(db), start_at, end_at)
    breached_expr = case(
        (
            (SlaClock.status == SlaClockStatus.breached.value)
            | SlaClock.breached_at.is_not(None),
            1,
        ),
        else_=0,
    )
    rows = (
        base.with_entities(
            func.date(SlaClock.started_at),
            func.count(SlaClock.id),
            func.sum(breached_expr),
        )
        .group_by(func.date(SlaClock.started_at))
        .order_by(func.date(SlaClock.started_at).asc())
        .all()
    )
    points: list[dict[str, Any]] = []
    for day_value, total, breached in rows:
        total_count = int(total or 0)
        breached_count = int(breached or 0)
        points.append(
            {
                "date": str(day_value),
                "total": total_count,
                "breached": breached_count,
                "breach_rate": round(
                    float(breached_count) / float(total_count) if total_count else 0.0,
                    4,
                ),
            }
        )
    return points


def violation_records(
    db: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    open_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List ticket SLA breach records for operational cleanup."""
    query = (
        db.query(SlaBreach, SlaClock, Ticket, ServiceTeam, SystemUser)
        .join(SlaClock, SlaClock.id == SlaBreach.clock_id)
        .join(Ticket, Ticket.id == SlaClock.entity_id)
        .outerjoin(ServiceTeam, ServiceTeam.id == Ticket.service_team_id)
        .outerjoin(SystemUser, SystemUser.id == Ticket.assigned_to_person_id)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(Ticket.is_active.is_(True))
    )
    if start_at:
        query = query.filter(SlaBreach.breached_at >= start_at)
    if end_at:
        query = query.filter(SlaBreach.breached_at <= end_at)
    if open_only:
        query = query.filter(SlaBreach.status != SlaBreachStatus.resolved.value)

    rows = (
        query.order_by(SlaBreach.breached_at.desc(), SlaBreach.created_at.desc())
        .limit(max(int(limit), 0))
        .all()
    )
    records: list[dict[str, Any]] = []
    for breach, clock, ticket, team, assignee in rows:
        ended_at = (
            clock.completed_at
            if breach.status == SlaBreachStatus.resolved.value
            else None
        )
        minutes = _duration_minutes(breach.breached_at, ended_at)
        reference = ticket.number or str(ticket.id)
        assignee_name = (
            assignee.display_name
            or " ".join(
                part for part in [assignee.first_name, assignee.last_name] if part
            ).strip()
            if assignee
            else ""
        )
        records.append(
            {
                "ticket_id": str(ticket.id),
                "ticket_reference": reference,
                "ticket_url": f"/admin/support/tickets/{reference}",
                "title": ticket.title,
                "status": ticket.status,
                "priority": ticket.priority,
                "region": ticket.region or "Unassigned",
                "service_team_id": str(ticket.service_team_id)
                if ticket.service_team_id
                else None,
                "service_team": team.name if team else "Unassigned",
                "assignee_person_id": str(ticket.assigned_to_person_id)
                if ticket.assigned_to_person_id
                else None,
                "assignee": assignee_name or "Unassigned",
                "sla_status": breach.status,
                "started_at": clock.started_at,
                "due_at": clock.due_at,
                "breached_at": breach.breached_at,
                "breach_minutes": minutes,
                "breach_duration": _duration_label(minutes),
            }
        )
    return records


def violation_page(
    db: Session, *, query: TicketSlaViolationPageQuery
) -> TicketSlaViolationPage:
    """Return one bounded page of ticket SLA breaches for the admin queue."""

    base = (
        db.query(SlaBreach, SlaClock, Ticket, ServiceTeam, SystemUser)
        .join(SlaClock, SlaClock.id == SlaBreach.clock_id)
        .join(Ticket, Ticket.id == SlaClock.entity_id)
        .outerjoin(ServiceTeam, ServiceTeam.id == Ticket.service_team_id)
        .outerjoin(SystemUser, SystemUser.id == Ticket.assigned_to_person_id)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(Ticket.is_active.is_(True))
    )
    if query.start_at:
        base = base.filter(SlaBreach.breached_at >= query.start_at)
    if query.end_at:
        base = base.filter(SlaBreach.breached_at <= query.end_at)
    if query.open_only:
        base = base.filter(SlaBreach.status != SlaBreachStatus.resolved.value)

    per_page = max(int(query.per_page), 1)
    total_count = int(base.count())
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(max(int(query.page), 1), total_pages)
    raw_rows = (
        base.order_by(SlaBreach.breached_at.desc(), SlaBreach.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    records: list[TicketSlaViolationRecord] = []
    for breach, clock, ticket, team, assignee in raw_rows:
        ended_at = (
            clock.completed_at
            if breach.status == SlaBreachStatus.resolved.value
            else None
        )
        minutes = _duration_minutes(breach.breached_at, ended_at)
        reference = ticket.number or str(ticket.id)
        assignee_name = (
            assignee.display_name
            or " ".join(
                part for part in [assignee.first_name, assignee.last_name] if part
            ).strip()
            if assignee
            else ""
        )
        records.append(
            TicketSlaViolationRecord(
                ticket_id=str(ticket.id),
                ticket_reference=reference,
                ticket_url=f"/admin/support/tickets/{reference}",
                title=ticket.title,
                status=ticket.status,
                priority=ticket.priority,
                region=ticket.region or "Unassigned",
                service_team_id=(
                    str(ticket.service_team_id) if ticket.service_team_id else None
                ),
                service_team=team.name if team else "Unassigned",
                assignee_person_id=(
                    str(ticket.assigned_to_person_id)
                    if ticket.assigned_to_person_id
                    else None
                ),
                assignee=assignee_name or "Unassigned",
                sla_status=breach.status,
                started_at=clock.started_at,
                due_at=clock.due_at,
                breached_at=breach.breached_at,
                breach_minutes=minutes,
                breach_duration=_duration_label(minutes),
            )
        )
    return TicketSlaViolationPage(
        rows=tuple(records),
        page=page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages,
    )


def build_violation_export_csv(db: Session, query: TicketSlaExportQuery) -> str:
    """Export the complete matching SLA violation projection."""

    records = violation_records(
        db,
        start_at=query.start_at,
        end_at=query.end_at,
        open_only=query.open_only,
        limit=10000,
    )
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "ticket_reference",
            "title",
            "status",
            "service_team",
            "assignee",
            "due_at",
            "breached_at",
            "breach_duration",
            "priority",
        ),
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(
        {
            key: (
                getattr(value, "value", value).isoformat()
                if isinstance(getattr(value, "value", value), datetime)
                else getattr(value, "value", value)
            )
            for key, value in record.items()
        }
        for record in records
    )
    return output.getvalue()
