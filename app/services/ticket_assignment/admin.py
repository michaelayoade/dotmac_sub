"""CRUD helpers for CRM-style ticket assignment rules (admin editor)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy


def list_rules(db: Session) -> list[TicketAssignmentRule]:
    """Return all rules in engine order (higher priority first)."""
    stmt = select(TicketAssignmentRule).order_by(
        TicketAssignmentRule.priority.desc(), TicketAssignmentRule.created_at.asc()
    )
    return list(db.scalars(stmt).all())


def get_rule(db: Session, rule_id: str | UUID) -> TicketAssignmentRule:
    rule = db.get(TicketAssignmentRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Assignment rule not found")
    return rule


def list_team_options(db: Session) -> list[dict[str, str]]:
    """Return active service teams (the FK target for rule.team_id)."""
    stmt = (
        select(ServiceTeam)
        .where(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.name.asc())
    )
    return [{"id": str(team.id), "label": team.name} for team in db.scalars(stmt).all()]


def create_rule(
    db: Session,
    *,
    name: str,
    priority: int = 0,
    strategy: str = TicketAssignmentStrategy.round_robin.value,
    match_config: dict[str, Any] | None = None,
    team_id: str | UUID | None = None,
    assign_manager: bool = False,
    assign_spc: bool = False,
    is_active: bool = True,
) -> TicketAssignmentRule:
    rule = TicketAssignmentRule(
        name=_clean_name(name),
        priority=priority,
        strategy=_clean_strategy(strategy),
        match_config=match_config or {},
        team_id=_coerce_team_id(team_id),
        assign_manager=assign_manager,
        assign_spc=assign_spc,
        is_active=is_active,
    )
    db.add(rule)
    db.flush()
    return rule


def update_rule(
    db: Session,
    rule_id: str | UUID,
    *,
    name: str,
    priority: int,
    strategy: str,
    match_config: dict[str, Any] | None,
    team_id: str | UUID | None,
    assign_manager: bool,
    assign_spc: bool,
    is_active: bool,
) -> TicketAssignmentRule:
    rule = get_rule(db, rule_id)
    rule.name = _clean_name(name)
    rule.priority = priority
    rule.strategy = _clean_strategy(strategy)
    rule.match_config = match_config or {}
    rule.team_id = _coerce_team_id(team_id)
    rule.assign_manager = assign_manager
    rule.assign_spc = assign_spc
    rule.is_active = is_active
    rule.updated_at = datetime.now(UTC)
    db.flush()
    return rule


def delete_rule(db: Session, rule_id: str | UUID) -> None:
    rule = get_rule(db, rule_id)
    db.delete(rule)
    db.flush()


def set_rule_active(
    db: Session, rule_id: str | UUID, *, is_active: bool
) -> TicketAssignmentRule:
    """Idempotent: explicitly enable/disable a rule."""
    rule = get_rule(db, rule_id)
    if rule.is_active != is_active:
        rule.is_active = is_active
        rule.updated_at = datetime.now(UTC)
        db.flush()
    return rule


# Legacy non-idempotent toggle kept for any callers that pass no target state.
def toggle_rule(db: Session, rule_id: str | UUID) -> TicketAssignmentRule:
    rule = get_rule(db, rule_id)
    return set_rule_active(db, rule_id, is_active=not rule.is_active)


def _clean_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("Assignment rule name is required.")
    return cleaned


def _clean_strategy(strategy: str) -> str:
    cleaned = str(strategy or "").strip()
    if cleaned not in {item.value for item in TicketAssignmentStrategy}:
        raise ValueError("strategy must be round_robin or least_loaded.")
    return cleaned


def _coerce_team_id(team_id: str | UUID | None) -> UUID | None:
    if team_id in (None, ""):
        return None
    if isinstance(team_id, UUID):
        return team_id
    try:
        return UUID(str(team_id).strip())
    except ValueError as exc:
        raise ValueError("team_id must be a valid UUID.") from exc
