"""Protect the vendor-submission confirmation coordinator boundary."""

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
OWNER = PROJECT_ROOT / "app" / "services" / "vendor_submission_proposals.py"
WEB_ADAPTER = PROJECT_ROOT / "app" / "web" / "vendor_portal.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _calls(path: Path) -> list[str]:
    tree = ast.parse(_source(path), filename=str(path))
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.append(node.func.attr)
    return calls


def test_confirmation_coordinator_has_a_complete_contract() -> None:
    service = service_relationship("operations.vendor_submission_confirmation")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(
        service,
        service_names={item.name for item in all_services()},
    )
    concerns = {item.name: item for item in service.contract.concerns}
    assert concerns["short-lived signed vendor submission proposal"].role is (
        OwnerRole.POLICY
    )
    assert concerns["vendor submission stale-preview verification"].role is (
        OwnerRole.POLICY
    )
    assert concerns["vendor submission idempotency and replay result"].role is (
        OwnerRole.APPLICATION_COORDINATOR
    )


def test_confirmation_owner_is_transport_and_transaction_neutral() -> None:
    source = _source(OWNER)
    calls = _calls(OWNER)

    assert calls.count("execute_owner_command") == 1
    for forbidden in (
        "fastapi",
        "HTTPException",
        "IntegrityError",
        ".commit(",
        ".rollback(",
        "begin_nested",
    ):
        assert forbidden not in source


def test_confirmation_locks_then_rechecks_replay_before_staleness() -> None:
    source = _source(OWNER)
    operation = source[source.index("    def operation()") :]

    assert "for_update=True" in operation
    assert operation.index("_locked_replay(") > operation.index("for_update=True")
    assert operation.index("_locked_replay(") < operation.index("hmac.compare_digest(")
    assert operation.index("IdempotencyKey(scope=scope, key=key)") > operation.index(
        "hmac.compare_digest("
    )
    assert operation.count("commit=False") == 4


def test_web_route_is_a_typed_error_mapping_adapter() -> None:
    source = _source(WEB_ADAPTER)
    confirmation_route = source[source.index("def vendor_confirm_submission(") :]

    assert "ConfirmVendorSubmissionCommand(" in confirmation_route
    assert "CommandContext(" in confirmation_route
    assert "db_session_adapter.release_read_transaction(db)" in confirmation_route
    assert "_submission_call(" in confirmation_route
    assert "VendorSubmissionError" in source
    assert ".commit(" not in confirmation_route
    assert ".rollback(" not in confirmation_route
