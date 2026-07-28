"""Protect reseller-scoped status decisions and confirmation coordination."""

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
OWNER = PROJECT_ROOT / "app" / "services" / "reseller_portal.py"
ADAPTER = PROJECT_ROOT / "app" / "services" / "web_reseller_routes.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _function_source(path: Path, *names: str) -> str:
    lines = _source(path).splitlines()
    selected: list[str] = []
    wanted = set(names)
    for node in _tree(path).body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in wanted
        ):
            assert node.end_lineno is not None
            selected.extend(lines[node.lineno - 1 : node.end_lineno])
    return "\n".join(selected)


def test_reseller_status_actions_have_a_complete_coordinator_contract() -> None:
    service = service_relationship("customer.reseller_status_actions")
    service_names = {item.name for item in all_services()}

    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(service, service_names=service_names)
    concerns = {item.name: item for item in service.contract.concerns}
    assert (
        concerns["reseller-scoped account-action impact preview"].role
        is OwnerRole.RESOLVER
    )
    assert concerns["lock-aware account-action eligibility"].role is OwnerRole.POLICY
    assert (
        concerns["account-action stale-preview fingerprint"].role is OwnerRole.RESOLVER
    )
    assert (
        concerns["account-bound idempotent status confirmation"].role
        is OwnerRole.APPLICATION_COORDINATOR
    )


def test_reseller_status_confirmation_has_one_owner_transaction_boundary() -> None:
    tree = _tree(OWNER)
    calls = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    ]
    status_slice = _function_source(
        OWNER,
        "_execute_status_action",
        "_reserve_customer_account_status_action",
        "_stage_customer_account_status_action",
        "_confirm_customer_account_status_action",
        "confirm_customer_account_status_action",
    )

    assert calls.count("execute_owner_command") == 1
    for forbidden in (
        "HTTPException",
        ".commit(",
        ".rollback(",
        "begin_nested",
        "commit: bool",
    ):
        assert forbidden not in status_slice
    assert "def update_customer_account_status(" not in _source(OWNER)


def test_reseller_confirmation_locks_and_rechecks_authoritative_state() -> None:
    reserve = _function_source(OWNER, "_reserve_customer_account_status_action")
    stage = _function_source(OWNER, "_stage_customer_account_status_action")

    assert ".with_for_update()" in reserve
    assert "existing.account_id != account.id" in reserve
    assert "existing.ref_id" in reserve
    confirmation = _function_source(OWNER, "_confirm_customer_account_status_action")
    assert "command.context.scope != str(command.account_id)" in confirmation
    assert stage.count(".with_for_update()") == 2
    assert stage.index("preview_customer_account_status_actions(") < stage.index(
        "secrets.compare_digest("
    )
    assert "suspend_subscription(" in stage
    assert "transition_subscription_status(" in stage
    assert "disable_subscription(" in stage
    assert "apply_requested_account_status(" in stage


def test_reseller_web_status_adapter_is_typed_and_transaction_neutral() -> None:
    adapter = _function_source(
        ADAPTER,
        "reseller_account_status_update",
        "reseller_account_status_confirm",
    )

    assert "PreviewCustomerAccountStatusConfirmationRequest(" in adapter
    assert "ConfirmCustomerAccountStatusCommand(" in adapter
    assert "ResellerAccountStatusAction(" in adapter
    assert "CommandContext.system(" in adapter
    assert "db_session_adapter.release_read_transaction(db)" in adapter
    assert "except DomainError as exc" in adapter
    assert ".commit(" not in adapter
    assert ".rollback(" not in adapter
