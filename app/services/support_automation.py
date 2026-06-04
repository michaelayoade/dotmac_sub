"""Service helpers for ticket automation rules.

A rule = trigger + conditions + action.
`apply_rules(db, ticket, trigger)` evaluates active rules in sort_order and
applies the action of every rule whose conditions match. Multiple rules can
fire on a single trigger event (e.g. one rule sets priority, another adds a
tag); per-rule failures are logged and recorded on the rule but do not block
other rules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    Ticket,
    TicketAutomationRule,
)
from app.services.customer_identity_resolution import (
    AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW,
    identity_resolution_requires_manual_review,
)

logger = logging.getLogger(__name__)

TICKET_CONDITION_FIELDS: tuple[str, ...] = (
    "status",
    "priority",
    "channel",
    "ticket_type",
    "region",
)


def list_rules(db: Session) -> list[TicketAutomationRule]:
    stmt = select(TicketAutomationRule).order_by(
        TicketAutomationRule.sort_order, TicketAutomationRule.created_at
    )
    return list(db.scalars(stmt).all())


def get_rule(db: Session, rule_id: str | UUID) -> TicketAutomationRule:
    rule = db.get(TicketAutomationRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Automation rule not found")
    return rule


def create_rule(
    db: Session,
    *,
    name: str,
    trigger: AutomationTrigger,
    action_type: AutomationActionType,
    conditions: dict[str, Any] | None = None,
    action_value: dict[str, Any] | None = None,
    description: str | None = None,
    sort_order: int = 100,
    is_active: bool = True,
) -> TicketAutomationRule:
    rule = TicketAutomationRule(
        name=name.strip(),
        description=(description or "").strip() or None,
        trigger=trigger,
        action_type=action_type,
        conditions=_clean_conditions(conditions or {}),
        action_value=action_value or {},
        sort_order=sort_order,
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
    trigger: AutomationTrigger,
    action_type: AutomationActionType,
    conditions: dict[str, Any] | None,
    action_value: dict[str, Any] | None,
    description: str | None,
    sort_order: int,
    is_active: bool,
) -> TicketAutomationRule:
    rule = get_rule(db, rule_id)
    rule.name = name.strip()
    rule.description = (description or "").strip() or None
    rule.trigger = trigger
    rule.action_type = action_type
    rule.conditions = _clean_conditions(conditions or {})
    rule.action_value = action_value or {}
    rule.sort_order = sort_order
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
) -> TicketAutomationRule:
    """Idempotent: explicitly enable/disable a rule."""
    rule = get_rule(db, rule_id)
    if rule.is_active != is_active:
        rule.is_active = is_active
        rule.updated_at = datetime.now(UTC)
        db.flush()
    return rule


# Legacy non-idempotent toggle kept for any callers that pass no target state.
def toggle_rule(db: Session, rule_id: str | UUID) -> TicketAutomationRule:
    rule = get_rule(db, rule_id)
    return set_rule_active(db, rule_id, is_active=not rule.is_active)


def apply_rules(db: Session, ticket: Ticket, trigger: AutomationTrigger) -> list[str]:
    """Apply matching automation rules to `ticket` for the given trigger.

    Iterates every active rule for `trigger` in sort_order. For each rule whose
    conditions match, applies the action. Failures on one rule are recorded on
    that rule and do not stop later rules. Returns the names of rules that
    fired successfully. Caller is responsible for committing.
    """
    if _identity_review_blocks_automation(ticket):
        _mark_identity_automation_suppressed(ticket)
        logger.warning(
            "ticket_automation_suppressed ticket_id=%s trigger=%s reason=%s",
            ticket.id,
            trigger.value if hasattr(trigger, "value") else trigger,
            AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW,
        )
        db.flush()
        return []

    stmt = (
        select(TicketAutomationRule)
        .where(
            TicketAutomationRule.is_active.is_(True),
            TicketAutomationRule.trigger == trigger,
        )
        .order_by(TicketAutomationRule.sort_order, TicketAutomationRule.created_at)
    )
    fired: list[str] = []
    now = datetime.now(UTC)
    for rule in db.scalars(stmt).all():
        if not _conditions_match(rule.conditions or {}, ticket):
            continue
        try:
            _apply_action(rule, ticket)
        except Exception as exc:
            rule.last_error = f"{type(exc).__name__}: {exc}"[:500]
            rule.last_error_at = now
            logger.exception(
                "automation_rule_apply_failed",
                extra={"rule_id": str(rule.id), "ticket_id": str(ticket.id)},
            )
            continue
        rule.last_fired_at = now
        rule.last_error = None
        rule.last_error_at = None
        fired.append(rule.name)
    db.flush()
    return fired


def _identity_review_blocks_automation(ticket: Ticket) -> bool:
    metadata = dict(ticket.metadata_ or {})
    return identity_resolution_requires_manual_review(
        metadata.get("identity_resolution")
    )


def _mark_identity_automation_suppressed(ticket: Ticket) -> None:
    metadata = dict(ticket.metadata_ or {})
    metadata["automation_paused"] = True
    metadata["ai_auto_actions_paused"] = True
    metadata["account_sensitive_automation_allowed"] = False
    metadata["automation_suppressed_reason"] = (
        AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW
    )
    ticket.metadata_ = metadata


def _clean_conditions(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values; keep only condition fields we know how to match."""
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in TICKET_CONDITION_FIELDS:
            continue
        if value in (None, ""):
            continue
        cleaned[key] = value
    return cleaned


def _conditions_match(conditions: dict[str, Any], ticket: Ticket) -> bool:
    for key, expected in conditions.items():
        actual = _ticket_field_value(ticket, key)
        if actual != expected:
            return False
    return True


def _ticket_field_value(ticket: Ticket, field: str) -> Any:
    value = getattr(ticket, field, None)
    if value is not None and hasattr(value, "value"):  # enum
        return value.value
    return value


def _apply_action(rule: TicketAutomationRule, ticket: Ticket) -> None:
    payload = rule.action_value or {}
    action = rule.action_type
    if action == AutomationActionType.assign_team:
        team_id = payload.get("service_team_id")
        if team_id:
            ticket.service_team_id = UUID(str(team_id))
    elif action == AutomationActionType.assign_technician:
        person_id = payload.get("technician_person_id")
        if person_id:
            ticket.technician_person_id = UUID(str(person_id))
    elif action == AutomationActionType.set_priority:
        value = payload.get("priority")
        if value:
            ticket.priority = str(value).strip()
    elif action == AutomationActionType.set_status:
        value = payload.get("status")
        if value:
            ticket.status = str(value).strip()
    elif action == AutomationActionType.set_due_in_hours:
        hours_raw = payload.get("hours")
        if hours_raw is not None:
            hours = int(hours_raw)
            ticket.due_at = datetime.now(UTC) + timedelta(hours=hours)
    elif action == AutomationActionType.add_tag:
        tag = payload.get("tag")
        if tag:
            tags = list(ticket.tags or [])
            if tag not in tags:
                tags.append(tag)
                ticket.tags = tags
