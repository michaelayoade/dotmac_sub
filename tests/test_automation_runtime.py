from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.automation import (
    AutomationRun,
    AutomationStepRun,
    AutomationStepStatus,
)
from app.services import automation_actions, automation_capabilities, automation_runtime
from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationDomainCapabilities,
    AutomationOperator,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.sot_registry.model import DomainSOT


@pytest.fixture
def declared_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    declaration = DomainSOT(
        domain="test_runtime_domain",
        services=(),
        entrypoints=(),
        rule="test only",
        automation=AutomationDomainCapabilities(
            target_types=("test.ticket",),
            triggers=(
                AutomationTriggerCapability(
                    key="test.ticket.created",
                    label="Ticket created",
                    event_type="subscriber.created",
                    event_schema_version=1,
                    entity_type="test.ticket",
                    tenant_id_field="tenant_id",
                    entity_id_field="ticket_id",
                    fields=(
                        AutomationConditionField(
                            key="priority",
                            label="Priority",
                            value_type=AutomationValueType.enum,
                            operators=(
                                AutomationOperator.equals,
                                AutomationOperator.in_values,
                            ),
                            enum_values=("normal", "urgent"),
                        ),
                        AutomationConditionField(
                            key="evidence.balance",
                            label="Balance",
                            value_type=AutomationValueType.decimal,
                            operators=(AutomationOperator.greater_than,),
                        ),
                    ),
                    author_permission="support:ticket:read",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        automation_capabilities, "DOMAIN_SOT_RELATIONSHIPS", (declaration,)
    )


def test_rule_conditions_use_only_declared_typed_fields(
    declared_trigger: None,
) -> None:
    assert automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=[
            {"field_key": "priority", "operator": "equals", "value": "urgent"},
            {
                "field_key": "evidence.balance",
                "operator": "greater_than",
                "value": "10.5",
            },
        ],
        payload={"priority": "urgent", "evidence": {"balance": "11.0"}},
    )


def test_undeclared_field_or_operator_fails_closed(declared_trigger: None) -> None:
    payload = {"priority": "urgent"}
    assert not automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=[{"field_key": "secret", "operator": "equals", "value": "x"}],
        payload=payload,
    )
    assert not automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=[
            {"field_key": "priority", "operator": "contains", "value": "urgent"}
        ],
        payload=payload,
    )


def test_rule_conditions_support_nested_or_and_not_groups(
    declared_trigger: None,
) -> None:
    conditions = {
        "group": "and",
        "children": [
            {
                "group": "or",
                "children": [
                    {"field_key": "priority", "operator": "equals", "value": "urgent"},
                    {
                        "group": "not",
                        "children": [
                            {
                                "field_key": "priority",
                                "operator": "equals",
                                "value": "normal",
                            }
                        ],
                    },
                ],
            }
        ],
    }
    assert automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=conditions,
        payload={"priority": "urgent"},
    )
    assert automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=conditions,
        payload={"priority": "low"},
    )
    assert not automation_runtime._rule_matches(
        trigger_key="test.ticket.created",
        conditions=conditions,
        payload={"priority": "normal"},
    )


def test_retry_preparation_skips_steps_that_already_succeeded() -> None:
    run_id = uuid4()
    run = AutomationRun(id=run_id)
    version = Mock()
    version.actions = [
        {"position": index, "inputs": [], "action_key": f"action.{index}"}
        for index in range(3)
    ]
    rows = [
        AutomationStepRun(
            id=uuid4(),
            run_id=run_id,
            step_index=0,
            action_key="action.0",
            status=AutomationStepStatus.succeeded.value,
        ),
        AutomationStepRun(
            id=uuid4(),
            run_id=run_id,
            step_index=1,
            action_key="action.1",
            status=AutomationStepStatus.failed.value,
        ),
        AutomationStepRun(
            id=uuid4(),
            run_id=run_id,
            step_index=2,
            action_key="action.2",
            status=AutomationStepStatus.blocked.value,
        ),
    ]
    db = Mock(spec=Session)
    db.scalars.return_value = rows

    prepared = automation_runtime._prepared_steps(db, run=run, version=version)

    assert [step.step_index for step in prepared] == [1, 2]
    assert [step.action_key for step in prepared] == ["action.1", "action.2"]


def test_run_history_list_contract_preserves_filter_and_page() -> None:
    query = automation_runtime.RUN_HISTORY_LIST.build_query(
        search=None,
        filters={"status": automation_runtime.AutomationRunStatus.failed.value},
        page=2,
        per_page=25,
    )

    assert query.filter_value("status") == "failed"
    assert query.offset == 25
    assert "status=failed" in query.url("/admin/automation/runs", page=3)


def test_execute_prepared_run_releases_action_read_transaction_before_finishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    event_id = uuid4()
    run_id = uuid4()
    step_id = uuid4()
    sequence: list[str] = []

    monkeypatch.setattr(
        automation_runtime,
        "claim_step",
        lambda db, command: automation_runtime.StepClaimOutcome(
            disposition=automation_runtime.StepClaimDisposition.execute,
            attempt_count=1,
        ),
    )
    monkeypatch.setattr(
        automation_capabilities,
        "action_capability",
        lambda action_key: SimpleNamespace(runtime_scope="test:automation"),
    )

    def execute_action(db, command):
        sequence.append("action")
        return automation_actions.AutomationActionOutcome(
            disposition=automation_actions.AutomationActionDisposition.succeeded,
            outcome_code="test_succeeded",
        )

    monkeypatch.setattr(
        automation_actions, "action_executor", lambda key: execute_action
    )
    monkeypatch.setattr(
        automation_runtime.db_session_adapter,
        "release_read_transaction",
        lambda db: sequence.append("release"),
    )

    def finish_step(db, command):
        sequence.append("finish")
        assert command.succeeded is True
        return automation_runtime.AutomationStepOutcome(
            run_id=run_id,
            step_id=step_id,
            step_status=automation_runtime.AutomationStepStatus.succeeded,
            run_status=automation_runtime.AutomationRunStatus.succeeded,
        )

    monkeypatch.setattr(automation_runtime, "finish_step", finish_step)

    result = automation_runtime.execute_prepared_run(
        Mock(spec=Session),
        automation_runtime.ExecutePreparedAutomationRunCommand(
            tenant_id=tenant_id,
            run=automation_runtime.PreparedAutomationRun(
                run_id=run_id,
                rule_id=uuid4(),
                rule_version_id=uuid4(),
                event_id=event_id,
                target=automation_actions.AutomationTargetReference(
                    entity_type="test.ticket", entity_id=uuid4()
                ),
                steps=(
                    automation_runtime.PreparedAutomationStep(
                        step_id=step_id,
                        step_index=0,
                        action_key="test.action",
                        inputs=(),
                    ),
                ),
            ),
            context=automation_runtime.CommandContext.system(
                actor="test",
                scope="test",
                reason="test automation runtime",
            ),
        ),
    )

    assert sequence == ["action", "release", "finish"]
    assert result.status is automation_runtime.AutomationRunStatus.succeeded
