from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services import automation_capabilities, automation_rules
from app.services.automation_actions import runtime_registry_errors
from app.services.automation_contracts import AutomationOperator
from app.services.events.handlers.automation import HANDLED_EVENT_TYPES
from app.services.events.types import EventType
from app.web.admin.automation_center import _pilot_rule_key


def test_ticket_assignment_pilot_is_draftable_but_not_runtime_enabled() -> None:
    trigger = automation_capabilities.trigger_capability("support.ticket.created")
    action = automation_capabilities.action_capability(
        "support.ticket.assign_service_team"
    )

    assert not trigger.runtime_enabled
    assert not action.runtime_enabled
    trigger_schema, conditions, actions = automation_rules._validate_definition(
        trigger_key=trigger.key,
        conditions=(
            automation_rules.AutomationCondition(
                field_key="priority",
                operator=AutomationOperator.equals,
                value="urgent",
            ),
        ),
        actions=(
            automation_rules.AutomationActionStep(
                action_key=action.key,
                inputs=(
                    automation_rules.AutomationActionValue(
                        key="service_team_id",
                        value=UUID("76a79707-c896-4db8-a802-6bce97cb0981"),
                    ),
                ),
            ),
        ),
        permission_keys=frozenset({"support:ticket:read", "support:ticket:update"}),
    )

    assert trigger_schema == 2
    assert conditions[0]["value"] == "urgent"
    assert actions[0]["action_key"] == action.key
    assert EventType.support_ticket_created not in HANDLED_EVENT_TYPES
    assert not runtime_registry_errors()


def test_ticket_assignment_pilot_cannot_be_published_before_runtime_admission() -> None:
    rule = SimpleNamespace(trigger_key="support.ticket.created")
    version = SimpleNamespace(conditions=[], actions=[])

    with pytest.raises(automation_rules.AutomationRuleError) as exc_info:
        automation_rules._validate_persisted_definition(
            db=SimpleNamespace(),
            rule=rule,
            version=version,
            permission_keys=frozenset({"support:ticket:read", "support:ticket:update"}),
        )

    assert (
        exc_info.value.code == "automation.rule_definitions.trigger_runtime_unavailable"
    )


def test_ticket_assignment_draft_key_is_stable_and_safe() -> None:
    assert _pilot_rule_key("Urgent tickets to Escalation Team") == (
        "support.ticket.assignment.urgent_tickets_to_escalation_team"
    )
