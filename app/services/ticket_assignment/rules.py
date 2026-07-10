"""Rule loading and matching for ticket/project auto-assignment.

Projects joined in Phase 3 (§2.1): rules gain an optional ``project_types``
match key and contexts carry ``entity_type ∈ {ticket, project}``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.support import Ticket
from app.models.ticket_workflow import TicketAssignmentRule


@dataclass(frozen=True)
class TicketAssignmentContext:
    entity_type: str
    priority: str | None
    ticket_type: str | None
    region: str | None
    source: str | None
    service_team_id: str | None
    tags: set[str]
    project_type: str | None = None


def list_active_rules(db: Session) -> list[TicketAssignmentRule]:
    """Return active rules ordered by priority then creation time."""
    return (
        db.query(TicketAssignmentRule)
        .filter(TicketAssignmentRule.is_active.is_(True))
        .order_by(
            TicketAssignmentRule.priority.desc(), TicketAssignmentRule.created_at.asc()
        )
        .all()
    )


def build_context(ticket: Ticket) -> TicketAssignmentContext:
    """Build normalized context used by rule matchers."""
    raw_tags = ticket.tags if isinstance(ticket.tags, list) else []
    tags = {str(value).strip().lower() for value in raw_tags if str(value).strip()}
    metadata = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
    return TicketAssignmentContext(
        entity_type="ticket",
        priority=str(ticket.priority or "").strip().lower() or None,
        ticket_type=str(ticket.ticket_type or "").strip().lower() or None,
        region=str(ticket.region or "").strip().lower() or None,
        source=ticket.channel.value
        if hasattr(ticket.channel, "value")
        else str(ticket.channel or "").strip() or None,
        service_team_id=str(ticket.service_team_id) if ticket.service_team_id else None,
        tags=tags | _metadata_tags(metadata),
    )


def build_project_context(project: Project) -> TicketAssignmentContext:
    """Build normalized context for project assignment rules (CRM parity)."""
    raw_tags = project.tags if isinstance(project.tags, list) else []
    tags = {str(value).strip().lower() for value in raw_tags if str(value).strip()}
    return TicketAssignmentContext(
        entity_type="project",
        priority=str(project.priority or "").strip().lower() or None,
        ticket_type=None,
        project_type=str(project.project_type or "").strip().lower() or None,
        region=str(project.region or "").strip().lower() or None,
        source=None,
        service_team_id=str(project.service_team_id)
        if project.service_team_id
        else None,
        tags=tags,
    )


def matches_rule(rule: TicketAssignmentRule, ctx: TicketAssignmentContext) -> bool:
    """Return True when a rule's match_config accepts the ticket context."""
    config = rule.match_config if isinstance(rule.match_config, dict) else {}
    if _not_in_list(config.get("entity_types"), ctx.entity_type):
        return False
    if _not_in_list(config.get("priorities"), ctx.priority):
        return False
    if ctx.entity_type == "ticket" and _not_in_list(
        config.get("ticket_types"), ctx.ticket_type
    ):
        return False
    if (ctx.entity_type == "project" or ctx.project_type) and _not_in_list(
        config.get("project_types"), ctx.project_type
    ):
        return False
    if _not_in_list(config.get("regions"), ctx.region):
        return False
    if _not_in_list(config.get("sources"), ctx.source):
        return False
    if _not_in_list(config.get("service_team_ids"), ctx.service_team_id):
        return False
    tags_any = _normalize_list(config.get("tags_any"))
    return not (tags_any and not (ctx.tags & set(tags_any)))


def _metadata_tags(metadata: dict) -> set[str]:
    raw = metadata.get("tags") if isinstance(metadata, dict) else None
    values = raw if isinstance(raw, list) else []
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _normalize_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip().lower()]
    return []


def _not_in_list(values: object, current: str | None) -> bool:
    options = _normalize_list(values)
    if not options:
        return False
    if current is None:
        return True
    return current.lower() not in options
