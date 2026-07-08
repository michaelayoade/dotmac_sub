"""Assignment strategy selectors for support ticket auto-assignment."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeamMember
from app.models.support import Ticket, TicketStatus
from app.models.ticket_workflow import TicketAssignmentCounter
from app.services.common import coerce_uuid


def list_team_candidate_person_ids(
    db: Session,
    team_id: str | None,
    *,
    max_open_tickets: int | None = None,
) -> list[str]:
    """List active team member person IDs for assignment candidates."""
    if not team_id:
        return []
    rows = (
        db.query(ServiceTeamMember.person_id)
        .filter(ServiceTeamMember.team_id == coerce_uuid(team_id))
        .filter(ServiceTeamMember.is_active.is_(True))
        .all()
    )
    candidates = sorted({str(row[0]) for row in rows if row[0] is not None})
    if max_open_tickets is not None and candidates:
        candidates = _filter_by_open_ticket_limit(db, candidates, max_open_tickets)
    return sorted(candidates)


def pick_least_loaded(db: Session, person_ids: list[str]) -> str | None:
    """Pick the candidate with the fewest open tickets."""
    if not person_ids:
        return None
    person_uuids = [coerce_uuid(pid) for pid in person_ids]
    counts = (
        db.query(Ticket.assigned_to_person_id, func.count(Ticket.id))
        .filter(Ticket.assigned_to_person_id.in_(person_uuids))
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(_open_ticket_statuses()))
        .group_by(Ticket.assigned_to_person_id)
        .all()
    )
    count_map = {str(row[0]): int(row[1]) for row in counts if row[0] is not None}
    return min(person_ids, key=lambda pid: (count_map.get(pid, 0), pid))


def pick_round_robin(db: Session, *, rule_id: str, person_ids: list[str]) -> str | None:
    """Pick the next candidate in rule-scoped round-robin order."""
    if not person_ids:
        return None
    ordered = sorted(person_ids)
    counter = (
        db.query(TicketAssignmentCounter)
        .filter(TicketAssignmentCounter.rule_id == coerce_uuid(rule_id))
        .first()
    )
    last = (
        str(counter.last_assigned_person_id)
        if counter and counter.last_assigned_person_id
        else None
    )
    next_person = ordered[0]
    if last and last in ordered:
        next_person = ordered[(ordered.index(last) + 1) % len(ordered)]
    if not counter:
        counter = TicketAssignmentCounter(rule_id=coerce_uuid(rule_id))
        db.add(counter)
    counter.last_assigned_person_id = coerce_uuid(next_person)
    db.flush()
    return next_person


def _open_ticket_statuses() -> list[str]:
    return [
        TicketStatus.new.value,
        TicketStatus.open.value,
        TicketStatus.pending.value,
        TicketStatus.waiting_on_customer.value,
        TicketStatus.on_hold.value,
        TicketStatus.lastmile_rerun.value,
        TicketStatus.site_under_construction.value,
    ]


def _filter_by_open_ticket_limit(
    db: Session, person_ids: list[str], max_open_tickets: int
) -> list[str]:
    if max_open_tickets < 0:
        return person_ids
    person_uuids = [coerce_uuid(pid) for pid in person_ids]
    counts = (
        db.query(Ticket.assigned_to_person_id, func.count(Ticket.id))
        .filter(Ticket.assigned_to_person_id.in_(person_uuids))
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(_open_ticket_statuses()))
        .group_by(Ticket.assigned_to_person_id)
        .all()
    )
    open_counts = {
        str(person_id): int(count)
        for person_id, count in counts
        if person_id is not None
    }
    return [
        person_id
        for person_id in person_ids
        if open_counts.get(person_id, 0) <= max_open_tickets
    ]
