"""Protect canonical FUP enforcement decisions and task boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_relationships import all_services, service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "fup_enforcement.py"
TASKS = PROJECT_ROOT / "app" / "tasks" / "usage.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.AST:
    return ast.parse(_source(path), filename=str(path))


def test_fup_enforcement_has_a_complete_coordinator_contract() -> None:
    service = service_relationship("access.fup_enforcement_sweep")
    service_names = {item.name for item in all_services()}

    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(service, service_names=service_names)
    concerns = {item.name: item for item in service.contract.concerns}
    assert (
        concerns["FUP sweep enforce/warn/reset decisions"].role
        is OwnerRole.APPLICATION_COORDINATOR
    )
    assert (
        concerns["FUP enforcement transition and cooldown hysteresis"].role
        is OwnerRole.POLICY
    )
    assert concerns["FUP repeat-upsell nudge policy"].role is OwnerRole.POLICY
    assert concerns["FUP customer notification fan-out"].role is OwnerRole.POLICY


def test_fup_enforcement_has_one_transport_neutral_transaction_boundary() -> None:
    source = _source(OWNER)
    calls = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(_tree(OWNER))
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    ]

    assert calls.count("execute_owner_command") == 1
    for forbidden in (
        "fastapi",
        "HTTPException",
        "SessionLocal",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "_resolve_or_create_quota_bucket",
    ):
        assert forbidden not in source
    assert ".with_for_update(" in source
    assert "fup_state.get_for_update(" in source


def test_fup_sweep_reads_metering_facts_without_creating_them() -> None:
    source = _source(OWNER)

    assert "def _current_quota_bucket(" in source
    assert "db.query(QuotaBucket)" in source
    assert "QuotaBucket(" not in source
    assert "db.add(bucket)" not in source


def test_fup_decision_stages_event_state_and_notification_in_one_operation() -> None:
    source = _source(OWNER)

    assert '"schema_version": 1' in source
    assert "stage_fup_runtime_state(" in source
    assert "_emit_enforcement_event(" in source
    assert "_emit_fup_notifications(db, pending_notifications)" in source
    assert "commit=False" in source


def test_fup_tasks_are_thin_typed_adapters() -> None:
    source = _source(TASKS)

    assert "RunFupSweepRequest(" in source
    assert "RunExpiredFupLiftRequest(" in source
    assert "run_fup_evaluation(" in source
    assert "run_expired_fup_lift(" in source
    for forbidden in (
        "_fup_should_enforce",
        "evaluate_rules(",
        "lift_fup_enforcement(",
        "list_pending_reset(",
        "_resolve_or_create_quota_bucket",
    ):
        assert forbidden not in source
