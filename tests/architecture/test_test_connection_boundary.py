"""Guard the actual owner, registered event path, and typed Finance action."""

import ast
from pathlib import Path

from app.services import automation_actions, automation_capabilities
from app.services.events.handlers.automation import HANDLED_EVENT_TYPES
from app.services.events.types import EventType
from app.services.sot_registry.registry import (
    registry_validation_errors,
    service_relationship,
)


def test_test_connection_is_a_registered_typed_workflow_capability() -> None:
    trigger = automation_capabilities.trigger_capability(
        "billing.test_connection.created"
    )
    action = automation_capabilities.action_capability(
        "billing.test_connection.notify_finance"
    )
    assert trigger.runtime_enabled and action.runtime_enabled
    assert trigger.entity_type == action.entity_type == "access.test_connection"
    assert "count_7d" in {field.key for field in trigger.fields}
    assert EventType.test_connection_created in HANDLED_EVENT_TYPES
    assert automation_actions.runtime_registry_errors() == ()
    assert registry_validation_errors() == ()
    assert service_relationship(action.command_owner).is_contracted


def test_finance_consequence_uses_one_owner_transaction_and_staff_participant() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "app/services/test_connection_finance.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    ]
    assert calls.count("execute_owner_command") == 1
    assert "stage_staff_direct_notification" in calls
    assert "resolve_assignment_users" in calls
    assert "stage_audit_event" in calls
    assert not any(
        call.endswith((".commit", ".rollback", ".begin_nested")) for call in calls
    )
    assert not any("smtp" in call.lower() or "send_email" in call for call in calls)
