"""CRM-style rule-based assignment engine for native tickets and projects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.project import Project, ProjectTask, ProjectTaskAssignee
from app.models.support import Ticket, TicketAssignee
from app.models.ticket_workflow import TicketAssignmentRule, TicketAssignmentStrategy
from app.services.common import coerce_uuid
from app.services.ticket_assignment.rules import (
    build_context,
    build_project_context,
    list_active_rules,
    matches_rule,
)
from app.services.ticket_assignment.selectors import (
    list_team_candidate_person_ids,
    pick_least_loaded,
    pick_round_robin,
)


@dataclass(frozen=True)
class AssignmentResult:
    assigned: bool
    ticket_id: str | None = None
    project_id: str | None = None
    rule_id: str | None = None
    rule_name: str | None = None
    strategy: str | None = None
    assignment_target: str | None = None
    candidate_count: int = 0
    assignee_person_id: str | None = None
    fallback_service_team_id: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "assigned": self.assigned,
            "ticket_id": self.ticket_id,
            "project_id": self.project_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "strategy": self.strategy,
            "assignment_target": self.assignment_target,
            "candidate_count": self.candidate_count,
            "assignee_person_id": self.assignee_person_id,
            "fallback_service_team_id": self.fallback_service_team_id,
            "reason": self.reason,
        }


def find_authoritative_project_creation_rule(
    db: Session, project: Project
) -> TicketAssignmentRule | None:
    """Return the first creation rule that owns initial assignment for a
    project (Phase 3 §2.1 — CRM engine parity). Accepts a transient Project
    probe (Projects.create builds one before INSERT)."""
    ctx = build_project_context(project)
    for rule in list_active_rules(db):
        if (
            matches_rule(rule, ctx)
            and _assignee_person_id(rule) is None
            and _has_authoritative_creation_scope(rule)
        ):
            return rule
    return None


def auto_assign_project(
    db: Session,
    project_id: str,
    *,
    trigger: str = "create",
    actor_person_id: str | None = None,
) -> list[AssignmentResult]:
    """Apply active workflow assignment rules to a project (CRM parity)."""
    del trigger
    del actor_person_id

    project = db.get(Project, coerce_uuid(project_id))
    if not project or not project.is_active:
        return [
            AssignmentResult(
                assigned=False,
                project_id=project_id,
                reason="project_not_found_or_inactive",
            )
        ]

    ctx = build_project_context(project)
    results: list[AssignmentResult] = []
    for rule in list_active_rules(db):
        if not matches_rule(rule, ctx):
            continue
        authoritative_result = _apply_authoritative_project_rule(
            db, project=project, rule=rule
        )
        if authoritative_result:
            results.append(authoritative_result)
            continue
        result = _apply_direct_project_assignment(db, project=project, rule=rule)
        if result:
            results.append(result)
    if not results:
        return [
            AssignmentResult(
                assigned=False,
                project_id=str(project.id),
                reason="no_matching_rule",
            )
        ]
    return results


def _apply_authoritative_project_rule(
    db: Session,
    *,
    project: Project,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    if _assignee_person_id(rule) or not _has_authoritative_creation_scope(rule):
        return None

    changed = False
    if rule.team_id and project.service_team_id != rule.team_id:
        project.service_team_id = rule.team_id
        changed = True
    if project.manager_person_id:
        project.manager_person_id = None
        changed = True
    if project.project_manager_person_id:
        project.project_manager_person_id = None
        changed = True
    if project.assistant_manager_person_id:
        project.assistant_manager_person_id = None
        changed = True

    if changed:
        db.flush()
    return AssignmentResult(
        assigned=bool(rule.team_id),
        project_id=str(project.id),
        rule_id=str(rule.id),
        rule_name=rule.name,
        strategy="group" if rule.team_id else None,
        assignment_target="team" if rule.team_id else "rule_scope",
        fallback_service_team_id=str(rule.team_id) if rule.team_id else None,
        reason="group_assigned"
        if rule.team_id
        else "individual_assignment_suppressed_by_rule",
    )


def _apply_direct_project_assignment(
    db: Session,
    *,
    project: Project,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    assignee = _assignee_person_id(rule)
    if not assignee:
        return None
    target = _assignment_target(rule)
    assignee_uuid = coerce_uuid(assignee)

    changed = False
    if target == "technical_supervisor":
        if not project.manager_person_id:
            project.manager_person_id = assignee_uuid
            changed = True
        if not project.project_manager_person_id:
            project.project_manager_person_id = assignee_uuid
            changed = True
    elif target == "site_coordinator":
        # assistant_manager ≡ "Site Project Coordinator" (Phase 3 §1.2).
        if not project.assistant_manager_person_id:
            project.assistant_manager_person_id = assignee_uuid
            changed = True
    elif target == "technician":
        tasks = (
            db.query(ProjectTask)
            .filter(ProjectTask.project_id == project.id)
            .filter(ProjectTask.is_active.is_(True))
            .all()
        )
        for task in tasks:
            if not task.assigned_to_person_id:
                task.assigned_to_person_id = assignee_uuid
                changed = True
            if not any(
                str(existing.person_id) == assignee for existing in task.assignees
            ):
                task.assignees.append(
                    ProjectTaskAssignee(task_id=task.id, person_id=assignee_uuid)
                )
                changed = True
    else:
        return AssignmentResult(
            assigned=False,
            project_id=str(project.id),
            rule_id=str(rule.id),
            rule_name=rule.name,
            assignment_target=target,
            assignee_person_id=assignee,
            reason="unsupported_assignment_target",
        )

    if changed:
        db.flush()
    return AssignmentResult(
        assigned=changed,
        project_id=str(project.id),
        rule_id=str(rule.id),
        rule_name=rule.name,
        strategy="direct",
        assignment_target=target,
        candidate_count=1,
        assignee_person_id=assignee,
        reason="assigned" if changed else "already_assigned",
    )


def auto_assign_ticket(
    db: Session,
    ticket_id: str,
    *,
    max_open_tickets: int | None = None,
) -> AssignmentResult:
    """Try to auto-assign a ticket using active CRM-style assignment rules."""
    results = auto_assign_ticket_all(db, ticket_id, max_open_tickets=max_open_tickets)
    assigned_result = next((result for result in results if result.assigned), None)
    return assigned_result or results[0]


def auto_assign_ticket_all(
    db: Session,
    ticket_id: str,
    *,
    max_open_tickets: int | None = None,
) -> list[AssignmentResult]:
    """Apply all compatible ticket assignment rules."""
    ticket = db.get(Ticket, coerce_uuid(ticket_id))
    if not ticket or not ticket.is_active:
        return [
            AssignmentResult(
                assigned=False,
                ticket_id=ticket_id,
                reason="ticket_not_found_or_inactive",
            )
        ]

    ctx = build_context(ticket)
    rules = list_active_rules(db)
    last_matched_rule: TicketAssignmentRule | None = None
    last_candidate_count = 0
    results: list[AssignmentResult] = []
    for rule in rules:
        if not matches_rule(rule, ctx):
            continue
        last_matched_rule = rule
        authoritative_result = _apply_authoritative_ticket_rule(
            db, ticket=ticket, rule=rule
        )
        if authoritative_result:
            results.append(authoritative_result)
            continue
        direct_result = _apply_direct_ticket_assignment(db, ticket=ticket, rule=rule)
        if direct_result:
            results.append(direct_result)
            continue
        if ticket.assigned_to_person_id:
            continue
        assignee, candidate_count = _select_assignee(
            db,
            ticket=ticket,
            rule=rule,
            max_open_tickets=max_open_tickets,
        )
        last_candidate_count = candidate_count
        if not assignee:
            if rule.team_id and not ticket.service_team_id:
                ticket.service_team_id = coerce_uuid(str(rule.team_id))
                db.flush()
                results.append(
                    AssignmentResult(
                        assigned=False,
                        ticket_id=str(ticket.id),
                        rule_id=str(rule.id),
                        rule_name=rule.name,
                        strategy=str(rule.strategy),
                        assignment_target="technician",
                        candidate_count=candidate_count,
                        fallback_service_team_id=str(rule.team_id),
                        reason="queue_fallback_team_assigned",
                    )
                )
                continue
            continue
        ticket.assigned_to_person_id = coerce_uuid(assignee)
        _ensure_assignee_row(db, ticket, assignee)
        db.flush()
        results.append(
            AssignmentResult(
                assigned=True,
                ticket_id=str(ticket.id),
                rule_id=str(rule.id),
                rule_name=rule.name,
                strategy=str(rule.strategy),
                assignment_target="technician",
                candidate_count=candidate_count,
                assignee_person_id=assignee,
                reason="assigned",
            )
        )
    if results:
        return results

    if last_matched_rule is not None:
        return [
            AssignmentResult(
                assigned=False,
                ticket_id=str(ticket.id),
                rule_id=str(last_matched_rule.id),
                rule_name=last_matched_rule.name,
                strategy=str(last_matched_rule.strategy),
                assignment_target=_assignment_target(last_matched_rule),
                candidate_count=last_candidate_count,
                assignee_person_id=str(ticket.assigned_to_person_id)
                if ticket.assigned_to_person_id
                else None,
                reason="already_assigned"
                if ticket.assigned_to_person_id
                else "no_eligible_candidates",
            )
        ]
    return [
        AssignmentResult(
            assigned=False, ticket_id=str(ticket.id), reason="no_matching_rule"
        )
    ]


def _select_assignee(
    db: Session,
    *,
    ticket: Ticket,
    rule: TicketAssignmentRule,
    max_open_tickets: int | None,
) -> tuple[str | None, int]:
    team_id = (
        str(rule.team_id)
        if rule.team_id
        else (str(ticket.service_team_id) if ticket.service_team_id else None)
    )
    candidates = list_team_candidate_person_ids(
        db, team_id, max_open_tickets=max_open_tickets
    )
    if not candidates:
        return None, 0
    if rule.strategy == TicketAssignmentStrategy.least_loaded.value:
        return pick_least_loaded(db, candidates), len(candidates)
    return pick_round_robin(db, rule_id=str(rule.id), person_ids=candidates), len(
        candidates
    )


def _assignment_config(rule: TicketAssignmentRule) -> dict:
    return rule.match_config if isinstance(rule.match_config, dict) else {}


def _assignment_target(rule: TicketAssignmentRule) -> str:
    return (
        str(_assignment_config(rule).get("assignment_target") or "technician")
        .strip()
        .lower()
    )


def _assignee_person_id(rule: TicketAssignmentRule) -> str | None:
    value = str(_assignment_config(rule).get("assignee_person_id") or "").strip()
    return value or None


def _has_authoritative_creation_scope(rule: TicketAssignmentRule) -> bool:
    config = _assignment_config(rule)
    return bool(
        config.get("entity_types")
        or config.get("ticket_types")
        or config.get("project_types")
        or config.get("regions")
        or config.get("service_team_ids")
        or config.get("tags_any")
    )


def _apply_authoritative_ticket_rule(
    db: Session,
    *,
    ticket: Ticket,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    if _assignee_person_id(rule) or not _has_authoritative_creation_scope(rule):
        return None

    changed = False
    if rule.team_id and ticket.service_team_id != rule.team_id:
        ticket.service_team_id = rule.team_id
        changed = True
    if ticket.assigned_to_person_id:
        ticket.assigned_to_person_id = None
        changed = True
    if ticket.ticket_manager_person_id:
        ticket.ticket_manager_person_id = None
        changed = True
    if ticket.site_coordinator_person_id:
        ticket.site_coordinator_person_id = None
        changed = True
    if ticket.assignees:
        ticket.assignees.clear()
        changed = True

    if changed:
        db.flush()
    return AssignmentResult(
        assigned=bool(rule.team_id),
        ticket_id=str(ticket.id),
        rule_id=str(rule.id),
        rule_name=rule.name,
        strategy="group" if rule.team_id else None,
        assignment_target="team" if rule.team_id else "rule_scope",
        fallback_service_team_id=str(rule.team_id) if rule.team_id else None,
        reason="group_assigned"
        if rule.team_id
        else "individual_assignment_suppressed_by_rule",
    )


def _apply_direct_ticket_assignment(
    db: Session,
    *,
    ticket: Ticket,
    rule: TicketAssignmentRule,
) -> AssignmentResult | None:
    assignee = _assignee_person_id(rule)
    if not assignee:
        return None
    assignee_uuid = coerce_uuid(assignee)
    target = _assignment_target(rule)
    changed = False
    if target == "technical_supervisor":
        if not ticket.ticket_manager_person_id:
            ticket.ticket_manager_person_id = assignee_uuid
            changed = True
    elif target == "site_coordinator":
        if not ticket.site_coordinator_person_id:
            ticket.site_coordinator_person_id = assignee_uuid
            changed = True
    elif target == "technician":
        if not ticket.assigned_to_person_id:
            ticket.assigned_to_person_id = assignee_uuid
            changed = True
        changed = _ensure_assignee_row(db, ticket, assignee) or changed
    else:
        return AssignmentResult(
            assigned=False,
            ticket_id=str(ticket.id),
            rule_id=str(rule.id),
            rule_name=rule.name,
            assignment_target=target,
            assignee_person_id=assignee,
            reason="unsupported_assignment_target",
        )

    if changed:
        db.flush()
    return AssignmentResult(
        assigned=changed,
        ticket_id=str(ticket.id),
        rule_id=str(rule.id),
        rule_name=rule.name,
        strategy="direct",
        assignment_target=target,
        candidate_count=1,
        assignee_person_id=assignee,
        reason="assigned" if changed else "already_assigned",
    )


def _ensure_assignee_row(db: Session, ticket: Ticket, person_id: str) -> bool:
    if any(str(existing.person_id) == person_id for existing in ticket.assignees):
        return False
    db.add(TicketAssignee(ticket_id=ticket.id, person_id=coerce_uuid(person_id)))
    return True
