"""Side-effect-free evaluation of Ticket automation policy."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    Ticket,
    TicketAutomationRule,
    TicketStatus,
    parse_ticket_status,
)
from app.models.ticket_workflow import TicketAssignmentRule
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.customer_identity_resolution import (
    AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW,
    identity_resolution_requires_manual_review,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TicketAutomationProposal:
    """Immutable consequence proposed by one matching automation rule."""

    rule_id: UUID
    rule_name: str
    action_type: AutomationActionType
    service_team_id: UUID | None = None
    technician_person_id: UUID | None = None
    priority: str | None = None
    status: TicketStatus | None = None
    due_in_hours: int | None = None
    tag: str | None = None


@dataclass(frozen=True, slots=True)
class AutomationCenterLegacyConflictQuery:
    """Automation Center actions and priority facts checked against legacy rules."""

    priority: str
    action_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class AutomationCenterLegacyRuleConflict:
    """One active legacy rule that could perform the same Ticket action."""

    surface_key: str
    rule_id: UUID
    rule_name: str


def _normalized_values(value: object) -> frozenset[str]:
    values = value if isinstance(value, list | tuple | set) else (value,)
    return frozenset(
        str(item).strip().casefold() for item in values if str(item).strip()
    )


def _allows_value(value: object, expected: str) -> bool:
    values = _normalized_values(value)
    return not values or expected.casefold() in values


def _assignment_rule_can_overlap_urgent_ticket(rule: TicketAssignmentRule) -> bool:
    """Conservatively identify active legacy rules that can set a Ticket team."""

    config = rule.match_config if isinstance(rule.match_config, dict) else {}
    if rule.team_id is None or config.get("assignee_person_id"):
        return False
    return _allows_value(config.get("entity_types"), "ticket") and _allows_value(
        config.get("priorities"), "urgent"
    )


def _automation_rule_can_overlap_urgent_ticket(rule: TicketAutomationRule) -> bool:
    if (
        rule.trigger is not AutomationTrigger.ticket_created
        or rule.action_type is not AutomationActionType.assign_team
    ):
        return False
    conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
    return _allows_value(conditions.get("priority"), "urgent")


def _automation_rule_can_overlap_priority_ticket(
    rule: TicketAutomationRule, *, priority: str
) -> bool:
    if (
        rule.trigger is not AutomationTrigger.ticket_created
        or rule.action_type is not AutomationActionType.set_priority
    ):
        return False
    conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
    return not priority or _allows_value(conditions.get("priority"), priority)


def list_automation_center_legacy_conflicts(
    db: Session,
    query: AutomationCenterLegacyConflictQuery,
) -> tuple[AutomationCenterLegacyRuleConflict, ...]:
    """Return live legacy overlap evidence for supported Ticket actions.

    The legacy rules remain separate owners. This check compares priority where
    both contracts expose it; region, type, channel, customer, and tag filters
    are treated as possibly overlapping when the contracts cannot prove them
    disjoint.
    """

    conflicts: list[AutomationCenterLegacyRuleConflict] = []
    if (
        "support.ticket.assign_service_team" in query.action_keys
        and query.priority.casefold() in {"", "urgent"}
        and support_ticket_settings_service.auto_assign_enabled(db)
    ):
        assignment_rules = db.scalars(
            select(TicketAssignmentRule)
            .where(TicketAssignmentRule.is_active.is_(True))
            .order_by(TicketAssignmentRule.created_at, TicketAssignmentRule.id)
        )
        conflicts.extend(
            AutomationCenterLegacyRuleConflict(
                surface_key="support.ticket_assignment_rules",
                rule_id=rule.id,
                rule_name=rule.name,
            )
            for rule in assignment_rules
            if _assignment_rule_can_overlap_urgent_ticket(rule)
        )
    check_assignment = (
        "support.ticket.assign_service_team" in query.action_keys
        and query.priority.casefold() in {"", "urgent"}
    )
    check_priority = "support.ticket.set_priority" in query.action_keys
    if not check_assignment and not check_priority:
        return ()
    automation_rules = db.scalars(
        select(TicketAutomationRule)
        .where(TicketAutomationRule.is_active.is_(True))
        .order_by(TicketAutomationRule.sort_order, TicketAutomationRule.id)
    )
    conflicts.extend(
        AutomationCenterLegacyRuleConflict(
            surface_key="support.ticket_creation_automation",
            rule_id=rule.id,
            rule_name=rule.name,
        )
        for rule in automation_rules
        if (check_assignment and _automation_rule_can_overlap_urgent_ticket(rule))
        or (
            check_priority
            and _automation_rule_can_overlap_priority_ticket(
                rule, priority=query.priority.casefold()
            )
        )
    )
    return tuple(conflicts)


def evaluate_rules(
    db: Session, ticket: Ticket, trigger: AutomationTrigger
) -> tuple[TicketAutomationProposal, ...]:
    """Return ordered proposals without mutating the Ticket or rule rows."""

    if identity_review_blocks_automation(ticket):
        logger.warning(
            "ticket_automation_suppressed ticket_id=%s trigger=%s reason=%s",
            ticket.id,
            trigger.value if hasattr(trigger, "value") else trigger,
            AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW,
        )
        return ()

    stmt = (
        select(TicketAutomationRule)
        .where(
            TicketAutomationRule.is_active.is_(True),
            TicketAutomationRule.trigger == trigger,
        )
        .order_by(TicketAutomationRule.sort_order, TicketAutomationRule.created_at)
    )
    proposals: list[TicketAutomationProposal] = []
    for rule in db.scalars(stmt).all():
        if not _conditions_match(rule.conditions or {}, ticket):
            continue
        try:
            proposal = _proposal_for_rule(rule)
        except (TypeError, ValueError):
            logger.exception(
                "automation_rule_evaluation_failed",
                extra={"rule_id": str(rule.id), "ticket_id": str(ticket.id)},
            )
            continue
        if proposal is not None:
            proposals.append(proposal)
    return tuple(proposals)


def identity_review_blocks_automation(ticket: Ticket) -> bool:
    metadata = dict(ticket.metadata_ or {})
    return identity_resolution_requires_manual_review(
        metadata.get("identity_resolution")
    )


def mark_identity_automation_suppressed(ticket: Ticket) -> None:
    """Participant helper called only by the canonical Ticket writer."""

    metadata = dict(ticket.metadata_ or {})
    metadata["automation_paused"] = True
    metadata["ai_auto_actions_paused"] = True
    metadata["account_sensitive_automation_allowed"] = False
    metadata["automation_suppressed_reason"] = (
        AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW
    )
    ticket.metadata_ = metadata


def _conditions_match(conditions: Mapping[str, object], ticket: Ticket) -> bool:
    for key, expected in conditions.items():
        actual = _ticket_field_value(ticket, key)
        if actual != expected:
            return False
    return True


def _ticket_field_value(ticket: Ticket, field: str) -> object:
    value = getattr(ticket, field, None)
    if value is not None and hasattr(value, "value"):
        return value.value
    return value


def _proposal_for_rule(
    rule: TicketAutomationRule,
) -> TicketAutomationProposal | None:
    payload = rule.action_value or {}
    action = rule.action_type
    if action == AutomationActionType.assign_team:
        team_id = payload.get("service_team_id")
        if team_id:
            return TicketAutomationProposal(
                rule_id=rule.id,
                rule_name=rule.name,
                action_type=action,
                service_team_id=UUID(str(team_id)),
            )
    elif action == AutomationActionType.assign_technician:
        person_id = payload.get("technician_person_id")
        if person_id:
            return TicketAutomationProposal(
                rule_id=rule.id,
                rule_name=rule.name,
                action_type=action,
                technician_person_id=UUID(str(person_id)),
            )
    elif action == AutomationActionType.set_priority:
        if value := payload.get("priority"):
            return TicketAutomationProposal(
                rule_id=rule.id,
                rule_name=rule.name,
                action_type=action,
                priority=str(value).strip(),
            )
    elif action == AutomationActionType.set_status:
        if value := payload.get("status"):
            return TicketAutomationProposal(
                rule_id=rule.id,
                rule_name=rule.name,
                action_type=action,
                status=parse_ticket_status(str(value).strip()),
            )
    elif action == AutomationActionType.set_due_in_hours:
        hours_raw = payload.get("hours")
        if hours_raw is not None:
            return TicketAutomationProposal(
                rule_id=rule.id,
                rule_name=rule.name,
                action_type=action,
                due_in_hours=int(hours_raw),
            )
    elif action == AutomationActionType.add_tag:
        if tag := payload.get("tag"):
            return TicketAutomationProposal(
                rule_id=rule.id,
                rule_name=rule.name,
                action_type=action,
                tag=str(tag),
            )
    return None
