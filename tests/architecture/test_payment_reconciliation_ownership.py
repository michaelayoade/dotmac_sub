"""Pin stranded top-up reconciliation to typed observation/consequence boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import (
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)

ROOT = Path(__file__).resolve().parents[2]
OWNER_PATH = ROOT / "app/services/payment_reconciliation.py"
TASK_PATH = ROOT / "app/tasks/payment_reconciliation.py"


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"), filename=str(OWNER_PATH))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _attribute_calls(node: ast.AST) -> set[str]:
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def _name_calls(node: ast.AST) -> set[str]:
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


def test_payment_reconciliation_has_complete_coordinator_contract() -> None:
    service = sot_relationships.service_relationship("financial.payment_reconciliation")

    assert service.module == "app.services.payment_reconciliation"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert {concern.role for concern in service.contract.concerns} == {
        OwnerRole.APPLICATION_COORDINATOR
    }
    assert (
        contract_validation_errors(
            service,
            service_names={item.name for item in sot_relationships.all_services()},
        )
        == ()
    )
    baseline = (ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt").read_text(
        encoding="utf-8"
    )
    assert "financial.payment_reconciliation" not in baseline.splitlines()


def test_each_reconciliation_consequence_is_one_typed_owner_command() -> None:
    source = OWNER_PATH.read_text(encoding="utf-8")
    verified = _function("settle_verified_reconciled_topup")
    unsuccessful = _function("project_unsuccessful_reconciled_topup")

    assert source.count("execute_owner_command(") == 2
    assert "execute_owner_command" in _name_calls(verified)
    assert "execute_owner_command" in _name_calls(unsuccessful)
    assert "CommandContext" in source
    assert "OwnerCommandDefinition" in source


def test_reconciliation_composes_named_flush_only_participants() -> None:
    verified = _function("_stage_verified_settlement")
    unsuccessful = _function("_stage_unsuccessful_observation")
    verified_calls = _attribute_calls(verified) | _name_calls(verified)
    unsuccessful_calls = _attribute_calls(unsuccessful) | _name_calls(unsuccessful)

    assert "stage_verified_settlement" in verified_calls
    assert "stage_verified_reconciliation_event" in verified_calls
    assert "stage_topup_intent_completion" in verified_calls
    assert "stage_topup_intent_expiry" in unsuccessful_calls
    for calls in (verified_calls, unsuccessful_calls):
        assert "commit" not in calls
        assert "rollback" not in calls


def test_sweep_separates_candidates_transport_and_consequence_transactions() -> None:
    source = OWNER_PATH.read_text(encoding="utf-8")
    sweep = _function("reconcile_pending_topups")
    candidates = _function("_reconciliation_candidates")
    sweep_calls = _attribute_calls(sweep) | _name_calls(sweep)

    assert "release_read_transaction" in sweep_calls
    assert "observe_verification" in sweep_calls
    assert "settle_verified_reconciled_topup" in sweep_calls
    assert "project_unsuccessful_reconciled_topup" in sweep_calls
    assert "SUPPORTED_PROVIDER_TYPES" in source
    assert "topup_reconciliation_batch_size" in ast.unparse(candidates)
    assert "_GATEWAY_PROVIDERS" not in source
    assert "_NOT_FOUND_STATUSES" not in source


def test_reconciliation_retires_parallel_financial_and_access_paths() -> None:
    source = OWNER_PATH.read_text(encoding="utf-8")

    for retired in (
        "reconcile_topups_scheduled",
        "restore_account_services",
        "settle_prepaid_draft_invoices_from_credit",
        "settle_verified_invoice_payment",
        "_provider_uuid",
        "SessionLocal",
        "_intent_allocations",
        "_settle_intent",
    ):
        assert retired not in source
    assert "Payment(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_task_owns_session_lifecycle_but_no_business_transaction() -> None:
    source = TASK_PATH.read_text(encoding="utf-8")

    assert "db_session_adapter.owner_command_session()" in source
    assert "RunTopupReconciliationCommand(" in source
    assert "reconcile_pending_topups(" in source
    assert ".as_dict()" in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "SessionLocal" not in source
