from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.models.support import TicketPriority
from app.services import automation_actions, automation_capabilities, automation_rules
from app.services.automation_actions import runtime_registry_errors
from app.services.automation_contracts import AutomationOperator
from app.services.events.handlers.automation import HANDLED_EVENT_TYPES
from app.services.events.types import EventType


def test_ticket_assignment_pilot_is_runtime_enabled() -> None:
    trigger = automation_capabilities.trigger_capability("support.ticket.created")
    action = automation_capabilities.action_capability(
        "support.ticket.assign_service_team"
    )

    assert trigger.runtime_enabled
    assert trigger.compatible_event_schema_versions == (3,)
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

    assert trigger_schema == 4
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

    assert trigger_schema == 4
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
        trigger_schema_version=4,
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


def test_ticket_builder_exposes_more_conditions_and_ordered_actions() -> None:
    trigger = automation_capabilities.trigger_capability("support.ticket.created")
    fields = {item.key for item in trigger.fields}
    assert {"priority", "ticket_type", "channel", "region", "customer_id"} <= fields
    assert automation_capabilities.action_capability(
        "support.ticket.set_priority"
    ).runtime_enabled
    _, _, actions = automation_rules._validate_definition(
        db=SimpleNamespace(),
        trigger_key=trigger.key,
        conditions=(),
        actions=(
            automation_rules.AutomationActionStep(
                action_key="support.ticket.set_priority",
                inputs=(automation_rules.AutomationActionValue("priority", "high"),),
            ),
            automation_rules.AutomationActionStep(
                action_key="support.ticket.assign_service_team",
                inputs=(
                    automation_rules.AutomationActionValue(
                        "service_team_id",
                        UUID("76a79707-c896-4db8-a802-6bce97cb0981"),
                    ),
                ),
            ),
        ),
        permission_keys=frozenset({"support:ticket:read", "support:ticket:update"}),
    )
    assert [item["position"] for item in actions] == [0, 1]


def test_priority_action_uses_typed_ticket_owner_command(monkeypatch) -> None:
    observed = {}

    def capture(_db, *, command):
        observed["command"] = command

    from app.services.support import Tickets

    monkeypatch.setattr(Tickets, "set_ticket_priority_from_automation", capture)
    command = automation_actions.ExecuteAutomationActionCommand(
        tenant_id=UUID("182a8f9e-52aa-4eb0-9912-85f830002a94"),
        event_id=UUID("76a79707-c896-4db8-a802-6bce97cb0981"),
        rule_id=UUID("98ce8c4d-71ca-42fa-9bb0-6a76d35c95e1"),
        rule_version_id=UUID("53c2d409-5f53-4c74-9b8c-2af4284bd731"),
        step_index=0,
        target=automation_actions.AutomationTargetReference(
            entity_type="support.ticket",
            entity_id=UUID("d7fac8aa-dce2-4447-91d8-94d46ab3c976"),
        ),
        inputs=(automation_actions.AutomationActionInputValue("priority", "high"),),
        context=SimpleNamespace(),
    )

    outcome = automation_actions.action_executor("support.ticket.set_priority")(
        SimpleNamespace(), command
    )

    assert outcome.outcome_code == "support_ticket_priority_set"
    assert observed["command"].priority is TicketPriority.high
    assert observed["command"].step_index == 0
