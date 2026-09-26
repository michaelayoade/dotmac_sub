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


def test_ticket_assignment_pilot_is_runtime_enabled() -> None:
    trigger = automation_capabilities.trigger_capability("support.ticket.created")
    action = automation_capabilities.action_capability(
        "support.ticket.assign_service_team"
    )

    assert trigger.runtime_enabled
    assert action.runtime_enabled
    trigger_schema, conditions, actions = automation_rules._validate_definition(
        db=SimpleNamespace(),
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

    assert trigger_schema == 3
    assert conditions[0]["value"] == "urgent"
    assert actions[0]["action_key"] == action.key
    assert EventType.support_ticket_created in HANDLED_EVENT_TYPES
    assert not runtime_registry_errors()
    assert automation_rules._runtime_ready(
        trigger_key=trigger.key,
        version=SimpleNamespace(actions=[{"action_key": action.key}]),
    )


def test_ticket_assignment_can_target_selected_customers(monkeypatch) -> None:
    customer_id = UUID("9d501e67-4252-45de-8b42-0e74f8a8e307")
    monkeypatch.setattr(
        automation_rules.customer_search,
        "get_customer_match",
        lambda _db, requested_id, *, active_only=False: (
            SimpleNamespace(id=requested_id) if active_only else None
        ),
    )
    trigger_schema, conditions, _actions = automation_rules._validate_definition(
        db=SimpleNamespace(),
        trigger_key="support.ticket.created",
        conditions=(
            automation_rules.AutomationCondition(
                field_key="customer_id",
                operator=AutomationOperator.in_values,
                value=(customer_id,),
            ),
        ),
        actions=(
            automation_rules.AutomationActionStep(
                action_key="support.ticket.assign_service_team",
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

    assert trigger_schema == 3
    assert conditions[0]["value"] == [str(customer_id)]


def test_ticket_assignment_rejects_an_inactive_selected_customer(monkeypatch) -> None:
    customer_id = UUID("9d501e67-4252-45de-8b42-0e74f8a8e307")
    monkeypatch.setattr(
        automation_rules.customer_search,
        "get_customer_match",
        lambda *_args, **_kwargs: None,
    )
    rule = SimpleNamespace(trigger_key="support.ticket.created")
    version = SimpleNamespace(
        trigger_schema_version=3,
        conditions=[
            {
                "field_key": "customer_id",
                "operator": "in",
                "value": [str(customer_id)],
            }
        ],
        actions=[],
    )

    with pytest.raises(automation_rules.AutomationRuleError) as exc_info:
        automation_rules._validate_persisted_definition(
            db=SimpleNamespace(),
            rule=rule,
            version=version,
            permission_keys=frozenset({"support:ticket:read", "support:ticket:update"}),
        )

    assert exc_info.value.code == "automation.rule_definitions.customer_scope_invalid"


def test_ticket_assignment_draft_key_is_stable_and_safe() -> None:
    assert _pilot_rule_key("Urgent tickets to Escalation Team") == (
        "support.ticket.assignment.urgent_tickets_to_escalation_team"
    )
