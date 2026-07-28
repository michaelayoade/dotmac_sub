"""Protect canonical FUP rule ownership and evaluation boundaries."""

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
OWNER = PROJECT_ROOT / "app" / "services" / "fup.py"
WEB_SERVICE = PROJECT_ROOT / "app" / "services" / "web_fup.py"
ADAPTER = PROJECT_ROOT / "app" / "web" / "admin" / "catalog.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.AST:
    return ast.parse(_source(path), filename=str(path))


def test_fup_rule_engine_has_a_complete_owner_contract() -> None:
    service = service_relationship("access.fup_rule_engine")
    service_names = {item.name for item in all_services()}

    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(service, service_names=service_names)
    concerns = {item.name: item for item in service.contract.concerns}
    assert (
        concerns["FUP policy and rule definitions (CRUD)"].role
        is OwnerRole.COMMAND_WRITER
    )
    assert concerns["FUP rule evaluation and simulation"].role is OwnerRole.POLICY


def test_fup_rule_owner_has_one_transport_neutral_transaction_boundary() -> None:
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
        ".commit(",
        ".rollback(",
        "begin_nested",
        "setattr(",
        "get_or_create",
        "**kwargs",
    ):
        assert forbidden not in source
    assert ".with_for_update(" in source
    assert "emit_event(" in source
    assert '"schema_version": 1' in source


def test_fup_admin_mutations_use_typed_commands_on_clean_sessions() -> None:
    web_service = _source(WEB_SERVICE)
    adapter = _source(ADAPTER)
    combined = f"{web_service}\n{adapter}"

    for command in (
        "UpdateFupPolicyCommand(",
        "AddFupRuleCommand(",
        "UpdateFupRuleCommand(",
        "DeleteFupRuleCommand(",
        "CloneFupRulesCommand(",
    ):
        assert command in combined
    assert web_service.count("release_read_transaction(db)") >= 5
    assert "except DomainError" in adapter
    assert ".commit(" not in web_service
    assert ".rollback(" not in web_service


def test_fup_configuration_get_is_read_only() -> None:
    web_service = _source(WEB_SERVICE)

    assert "fup_policies.get_by_offer(db, offer_id)" in web_service
    assert "fup_policies.get_or_create" not in web_service
