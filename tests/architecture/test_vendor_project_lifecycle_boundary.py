"""Protect the vendor project lifecycle participant boundary."""

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
APP_ROOT = PROJECT_ROOT / "app"
OWNER = APP_ROOT / "services" / "vendor_project_lifecycle.py"
ALLOWED_CALLERS = {"app/services/vendor_submission_proposals.py"}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_lifecycle_participant_has_a_complete_contract() -> None:
    service = service_relationship("operations.vendor_project_lifecycle")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(
        service,
        service_names={item.name for item in all_services()},
    )
    concerns = {item.name: item for item in service.contract.concerns}
    assert concerns["vendor start/complete installation-project transitions"].role is (
        OwnerRole.COMMAND_WRITER
    )
    assert concerns["durable vendor lifecycle actor/time/event evidence"].role is (
        OwnerRole.AUTHORITATIVE_RECORD
    )


def test_lifecycle_participant_only_flushes_and_stages_events() -> None:
    source = OWNER.read_text(encoding="utf-8")

    for forbidden in (
        "fastapi",
        "HTTPException",
        ".commit(",
        ".rollback(",
        "execute_owner_command",
        "commit: bool",
    ):
        assert forbidden not in source
    assert ".with_for_update(" in source
    assert "emit_event(" in source
    assert '"schema_version": 1' in source


def test_lifecycle_participant_has_only_the_named_coordinator_caller() -> None:
    callers: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        if path == OWNER:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name == "stage_project_transition":
                callers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert callers == ALLOWED_CALLERS


def test_workspace_no_longer_writes_lifecycle_state_or_evidence() -> None:
    source = (APP_ROOT / "services" / "vendor_portal_operations.py").read_text(
        encoding="utf-8"
    )

    assert "InstallationProjectLifecycleEvent" not in source
    assert "EventType.vendor_project_" not in source
    assert "def transition_project(" not in source
    assert "def start_project(" not in source
    assert "def complete_project(" not in source
