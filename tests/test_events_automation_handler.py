from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.events.handlers import automation
from app.services.events.types import Event, EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID


@pytest.mark.parametrize("scheduled", (False, True))
def test_custom_event_uses_registered_name_for_runtime_trigger(monkeypatch, scheduled):
    event_name = (
        "operations.work_order.scheduled" if scheduled else "work_order.created"
    )
    version_id = uuid4()
    event = Event(
        event_type=EventType.custom,
        payload={
            "name": event_name,
            "tenant_id": str(OPERATOR_TENANT_ID),
            "work_order_id": str(uuid4()),
            "automation_rule_version_id": str(version_id),
        },
    )
    trigger = SimpleNamespace(
        key=event_name,
        event_type=event_name,
        entity_type="operations.work_order",
        tenant_id_field="tenant_id",
        entity_id_field="work_order_id",
        scheduled=scheduled,
    )
    prepared_commands = []

    monkeypatch.setattr(
        automation, "HANDLED_EVENT_TYPES", frozenset({EventType.custom})
    )
    monkeypatch.setattr(automation, "_registered_triggers", lambda: (trigger,))
    monkeypatch.setattr(automation, "_registered_script_targets", lambda: ())
    monkeypatch.setattr(
        automation.AutomationEventHandler,
        "_execute_server_scripts",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(automation, "owner_session", lambda db: nullcontext(db))

    def prepare_event_runs(db, command):
        prepared_commands.append(command)
        return ()

    monkeypatch.setattr(
        automation.automation_runtime, "prepare_event_runs", prepare_event_runs
    )

    automation.AutomationEventHandler().handle(object(), event)

    assert len(prepared_commands) == 1
    assert prepared_commands[0].event.event_type == event_name
    assert prepared_commands[0].event.scheduled_rule_version_id == (
        version_id if scheduled else None
    )


def test_execute_prepared_run_preserves_non_retryable_action_failure(monkeypatch):
    event = Event(event_type=EventType.custom, payload={})
    run = SimpleNamespace(run_id=uuid4())

    monkeypatch.setattr(automation, "owner_session", lambda db: nullcontext(db))
    monkeypatch.setattr(
        automation.automation_runtime,
        "execute_prepared_run",
        lambda db, command: SimpleNamespace(
            error_code="support.ticket_sla_service_consequence.customer_account_missing",
            retryable=False,
        ),
    )

    with pytest.raises(automation.AutomationEventHandlerError) as exc_info:
        automation.AutomationEventHandler().execute_prepared_run(
            object(),
            event=event,
            run=run,
            tenant_id=OPERATOR_TENANT_ID,
        )

    assert exc_info.value.retryable is False
    assert exc_info.value.code == "automation.execution.event_handler_failed"
