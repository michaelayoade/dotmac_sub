"""CRUD helpers for CRM-style ticket assignment rules (admin editor)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.service_team import ServiceTeam
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

_OWNER = "support.ticket_assignment_rule_configuration"
_CONCERN = "ticket assignment-rule configuration"


class TicketAssignmentRuleError(DomainError):
    """Transport-neutral assignment-rule configuration error."""


class TicketAssignmentTarget(StrEnum):
    TECHNICIAN = "technician"
    TECHNICAL_SUPERVISOR = "technical_supervisor"
    SITE_COORDINATOR = "site_coordinator"


@dataclass(frozen=True)
class TicketAssignmentRuleMatch:
    entity_types: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()
    ticket_types: tuple[str, ...] = ()
    project_types: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    service_team_ids: tuple[UUID, ...] = ()
    tags_any: tuple[str, ...] = ()
    assignee_person_id: UUID | None = None
    assignment_target: TicketAssignmentTarget | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for field in (
            "entity_types",
            "priorities",
            "ticket_types",
            "project_types",
            "regions",
            "sources",
            "tags_any",
        ):
            values = getattr(self, field)
            if values:
                result[field] = list(values)
        if self.service_team_ids:
            result["service_team_ids"] = [str(value) for value in self.service_team_ids]
        if self.assignee_person_id is not None:
            result["assignee_person_id"] = str(self.assignee_person_id)
            result["assignment_target"] = str(
                self.assignment_target or TicketAssignmentTarget.TECHNICIAN
            )
        return result


def _configuration_command(name: str):
    definition = OwnerCommandDefinition(owner=_OWNER, concern=_CONCERN, name=name)

    def decorate(operation):
        @wraps(operation)
        def wrapped(db: Session, *args, **kwargs):
            if owner_command_active(db, owner=_OWNER):
                return operation(db, *args, **kwargs)
            context = CommandContext.system(
                actor="support-assignment-admin",
                scope=f"support.ticket_assignment_rule:{name}",
                reason=f"change Ticket assignment rule via {name}",
            )

            def apply():
                result = operation(db, *args, **kwargs)
                entity_id = getattr(result, "id", None)
                if entity_id is None and args:
                    entity_id = args[0]
                stage_audit_event(
                    db,
                    action="ticket.assignment_rule_changed",
                    entity_type="ticket_assignment_rule",
                    entity_id=str(entity_id) if entity_id else None,
                    actor_type=AuditActorType.system,
                    metadata={"owner": _OWNER, "operation": name},
                )
                return result

            return execute_owner_command(
                db, definition=definition, context=context, operation=apply
            )

        return wrapped

    return decorate


def list_rules(db: Session) -> list[TicketAssignmentRule]:
    """Return all rules in engine order (higher priority first)."""
    stmt = select(TicketAssignmentRule).order_by(
        TicketAssignmentRule.priority.desc(), TicketAssignmentRule.created_at.asc()
    )
    return list(db.scalars(stmt).all())


def get_rule(db: Session, rule_id: str | UUID) -> TicketAssignmentRule:
    rule = db.get(TicketAssignmentRule, rule_id)
    if rule is None:
        raise TicketAssignmentRuleError(
            code="assignment_rule_not_found",
            message="Assignment rule not found",
        )
    return rule


def list_team_options(db: Session) -> list[dict[str, str]]:
    """Return active service teams (the FK target for rule.team_id)."""
    stmt = (
        select(ServiceTeam)
        .where(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.name.asc())
    )
    return [{"id": str(team.id), "label": team.name} for team in db.scalars(stmt).all()]


@_configuration_command("create_assignment_rule")
def create_rule(
    db: Session,
    *,
    name: str,
    priority: int = 0,
    strategy: str = TicketAssignmentStrategy.round_robin.value,
    match_config: TicketAssignmentRuleMatch | None = None,
    team_id: str | UUID | None = None,
    assign_manager: bool = False,
    assign_spc: bool = False,
    is_active: bool = True,
) -> TicketAssignmentRule:
    rule = TicketAssignmentRule(
        name=_clean_name(name),
        priority=priority,
        strategy=_clean_strategy(strategy),
        match_config=(match_config or TicketAssignmentRuleMatch()).as_dict(),
        team_id=_coerce_team_id(team_id),
        assign_manager=assign_manager,
        assign_spc=assign_spc,
        is_active=is_active,
    )
    db.add(rule)
    db.flush()
    return rule


@_configuration_command("update_assignment_rule")
def update_rule(
    db: Session,
    rule_id: str | UUID,
    *,
    name: str,
    priority: int,
    strategy: str,
    match_config: TicketAssignmentRuleMatch | None,
    team_id: str | UUID | None,
    assign_manager: bool,
    assign_spc: bool,
    is_active: bool,
) -> TicketAssignmentRule:
    rule = get_rule(db, rule_id)
    rule.name = _clean_name(name)
    rule.priority = priority
    rule.strategy = _clean_strategy(strategy)
    rule.match_config = (match_config or TicketAssignmentRuleMatch()).as_dict()
    rule.team_id = _coerce_team_id(team_id)
    rule.assign_manager = assign_manager
    rule.assign_spc = assign_spc
    rule.is_active = is_active
    rule.updated_at = datetime.now(UTC)
    db.flush()
    return rule


@_configuration_command("delete_assignment_rule")
def delete_rule(db: Session, rule_id: str | UUID) -> None:
    rule = get_rule(db, rule_id)
    db.delete(rule)
    db.flush()


@_configuration_command("set_assignment_rule_active")
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
@_configuration_command("toggle_assignment_rule")
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
