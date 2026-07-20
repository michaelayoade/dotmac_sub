"""SLA clock service for support tickets."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.support import Ticket, TicketStatus
from app.models.ticket_workflow import (
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    WorkflowEntityType,
)
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

DEFAULT_TICKET_SLA_POLICY_NAME = "Ticket Resolution SLA"

CUSTOMER_AND_CABINET_TICKET_TYPES_24H = frozenset(
    {
        "customer link disconnection",
        "multiple customer link disconnection",
        "customer realignment",
        "cabinet disconnection",
        "multiple cabinet link disconnection",
        "multiple cabinet disconnection",
        "cabinet migration",
    }
)
CORE_LINK_TICKET_TYPES_48H = frozenset(
    {
        "core link disconnection",
        "multiple core link disconnection",
    }
)

SLA_COMPLETE_STATUSES = frozenset(
    {
        TicketStatus.resolved.value,
        TicketStatus.pending_confirmation.value,
        TicketStatus.closed.value,
        TicketStatus.canceled.value,
        TicketStatus.merged.value,
    }
)
SLA_APPLICABLE_STATUSES = frozenset(
    {
        TicketStatus.new.value,
        TicketStatus.open.value,
        TicketStatus.pending.value,
        TicketStatus.lastmile_rerun.value,
        TicketStatus.waiting_on_customer.value,
        TicketStatus.on_hold.value,
        TicketStatus.site_under_construction.value,
    }
)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_sla_policy(db: Session, ticket: Ticket) -> SlaPolicy | None:
    """Find the explicit default ticket SLA policy for a ticket."""
    policy = (
        db.query(SlaPolicy)
        .filter(SlaPolicy.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaPolicy.is_active.is_(True))
        .filter(func.lower(SlaPolicy.name) == DEFAULT_TICKET_SLA_POLICY_NAME.lower())
        .first()
    )
    if policy:
        return policy

    logger.warning(
        "ticket_sla_policy_not_found ticket_id=%s expected_policy_name=%s",
        getattr(ticket, "id", None),
        DEFAULT_TICKET_SLA_POLICY_NAME,
    )
    return None


def ticket_type_sla_target_minutes(ticket_type: str | None) -> int | None:
    """Return fixed SLA target minutes for known infrastructure ticket types."""
    normalized = " ".join(str(ticket_type or "").strip().lower().split())
    if normalized in CUSTOMER_AND_CABINET_TICKET_TYPES_24H:
        return 24 * 60
    if normalized in CORE_LINK_TICKET_TYPES_48H:
        return 48 * 60
    return None


def priority_sla_target_minutes(db: Session, priority: str | None) -> int | None:
    """Return configured support resolution target minutes for a priority."""
    normalized = str(priority or "").strip().lower()
    if not normalized:
        return None
    policy = support_ticket_settings_service.sla_policy(db).get(normalized, {})
    resolution_hours = int(policy.get("resolution_hours") or 0)
    if resolution_hours <= 0:
        return None
    return resolution_hours * 60


def resolve_ticket_sla_target_minutes(
    db: Session,
    *,
    priority: str | None,
    ticket_type: str | None,
) -> int | None:
    """Resolve a ticket SLA target, preferring explicit operational windows."""
    return ticket_type_sla_target_minutes(ticket_type) or priority_sla_target_minutes(
        db, priority
    )


def create_sla_clock_for_ticket(db: Session, ticket: Ticket) -> SlaClock | None:
    """Create an SLA clock for a ticket when a support SLA policy applies."""
    policy = resolve_sla_policy(db, ticket)
    if not policy:
        return None
    if str(ticket.status or "") not in SLA_APPLICABLE_STATUSES:
        return None

    explicit_type_target = ticket_type_sla_target_minutes(ticket.ticket_type)
    target_minutes = explicit_type_target or priority_sla_target_minutes(
        db, ticket.priority
    )
    if target_minutes is None:
        return None

    existing = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id == ticket.id)
        .filter(SlaClock.policy_id == policy.id)
        .first()
    )
    if existing:
        return existing

    started_at = _as_aware_utc(ticket.created_at) or datetime.now(UTC)
    due_at = (
        started_at + timedelta(minutes=target_minutes)
        if explicit_type_target is not None or ticket.due_at is None
        else _as_aware_utc(ticket.due_at)
    )
    if due_at is None:
        due_at = started_at + timedelta(minutes=target_minutes)
    clock = SlaClock(
        policy_id=policy.id,
        entity_type=WorkflowEntityType.ticket.value,
        entity_id=ticket.id,
        priority=str(ticket.priority or "") or None,
        status=SlaClockStatus.running.value,
        started_at=started_at,
        due_at=due_at,
    )
    db.add(clock)
    return clock


def latest_ticket_sla_clocks(
    db: Session, ticket_ids: Iterable[UUID | str]
) -> dict[str, SlaClock]:
    """Return the newest SLA clock per ticket id."""
    ids = [coerce_uuid(str(ticket_id)) for ticket_id in ticket_ids if ticket_id]
    ids = [ticket_id for ticket_id in ids if ticket_id is not None]
    if not ids:
        return {}
    clocks = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id.in_(ids))
        .order_by(SlaClock.entity_id.asc(), SlaClock.created_at.desc())
        .all()
    )
    latest: dict[str, SlaClock] = {}
    for clock in clocks:
        latest.setdefault(str(clock.entity_id), clock)
    return latest


def ticket_sla_status(
    ticket: Ticket,
    *,
    clock: SlaClock | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build the read model used by support ticket UI/reporting."""
    current = _as_aware_utc(now) or datetime.now(UTC)
    due_at = _as_aware_utc(clock.due_at if clock else ticket.due_at)
    created_at = _as_aware_utc(ticket.created_at)
    terminal = str(ticket.status or "") in SLA_COMPLETE_STATUSES
    clock_breached = bool(
        clock
        and (
            clock.status == SlaClockStatus.breached.value
            or clock.breached_at is not None
        )
    )
    overdue = bool(due_at and due_at < current and not terminal)
    minutes_remaining = (
        int((due_at - current).total_seconds() // 60) if due_at is not None else None
    )
    return {
        "status": clock.status if clock else None,
        "priority": clock.priority if clock else ticket.priority,
        "started_at": clock.started_at if clock else created_at,
        "due_at": due_at,
        "breached": clock_breached or overdue,
        "breached_at": clock.breached_at if clock else None,
        "minutes_remaining": minutes_remaining,
        "age_hours": int((current - created_at).total_seconds() // 3600)
        if created_at
        else 0,
        "terminal": terminal,
    }


def update_sla_clocks_for_status_change(
    db: Session,
    ticket: Ticket,
    old_status: str | None,
    new_status: str,
) -> None:
    """Update SLA clocks when a ticket status changes."""
    del old_status
    clocks = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id == ticket.id)
        .filter(
            SlaClock.status.in_(
                [
                    SlaClockStatus.running.value,
                    SlaClockStatus.paused.value,
                    SlaClockStatus.breached.value,
                ]
            )
        )
        .all()
    )
    if not clocks:
        return

    now = datetime.now(UTC)
    for clock in clocks:
        if new_status in SLA_COMPLETE_STATUSES:
            clock.status = SlaClockStatus.completed.value
            clock.completed_at = now
            clock.paused_at = None
            open_breaches = (
                db.query(SlaBreach)
                .filter(SlaBreach.clock_id == clock.id)
                .filter(SlaBreach.status != SlaBreachStatus.resolved.value)
                .all()
            )
            for breach in open_breaches:
                breach.status = SlaBreachStatus.resolved.value
        elif new_status in SLA_APPLICABLE_STATUSES:
            clock.completed_at = None
            clock.paused_at = None
            if clock.status == SlaClockStatus.paused.value:
                clock.status = SlaClockStatus.running.value


def check_sla_breaches(db: Session, ticket_id) -> list[SlaClock]:
    """Check for SLA breaches on a ticket's running clocks."""
    now = datetime.now(UTC)
    ticket = db.get(Ticket, coerce_uuid(str(ticket_id)))
    if not ticket or str(ticket.status or "") not in SLA_APPLICABLE_STATUSES:
        return []
    clocks = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id == ticket.id)
        .filter(SlaClock.status == SlaClockStatus.running.value)
        .filter(SlaClock.due_at < now)
        .filter(SlaClock.breached_at.is_(None))
        .all()
    )

    breached = []
    for clock in clocks:
        due_at = (
            clock.due_at if clock.due_at.tzinfo else clock.due_at.replace(tzinfo=UTC)
        )
        clock.status = SlaClockStatus.breached.value
        clock.breached_at = due_at
        db.add(
            SlaBreach(
                clock_id=clock.id, status=SlaBreachStatus.open.value, breached_at=due_at
            )
        )
        breached.append(clock)

    return breached
