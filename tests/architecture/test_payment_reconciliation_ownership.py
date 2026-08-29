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
        OwnerRole.APPLICATION_COORDINATOR,
        OwnerRole.AUTHORITATIVE_RECORD,
        OwnerRole.RESOLVER,
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


def test_each_reconciliation_boundary_is_one_typed_owner_command() -> None:
    source = OWNER_PATH.read_text(encoding="utf-8")
    verified = _function("settle_verified_reconciled_topup")
    observation = _function("record_reconciled_gateway_observation")
    attempt = _function("claim_topup_reconciliation_attempt")
    outside_window = _function("confirm_paystack_outside_window_recovery")

    assert source.count("execute_owner_command(") == 4
    assert "execute_owner_command" in _name_calls(verified)
    assert "execute_owner_command" in _name_calls(observation)
    assert "execute_owner_command" in _name_calls(attempt)
    assert "execute_owner_command" in _name_calls(outside_window)
    assert "CommandContext" in source
    assert "OwnerCommandDefinition" in source


def test_reconciliation_composes_named_flush_only_participants() -> None:
    verified = _function("_stage_verified_settlement")
    observation = _function("_stage_gateway_observation")
    verified_calls = _attribute_calls(verified) | _name_calls(verified)
    observation_calls = _attribute_calls(observation) | _name_calls(observation)

    assert "stage_verified_settlement" in verified_calls
    assert "stage_verified_reconciliation_event" in verified_calls
    assert "stage_topup_intent_completion" in verified_calls
    assert "stage_gateway_topup_observation" in observation_calls
    for calls in (verified_calls, observation_calls):
        assert "commit" not in calls
        assert "rollback" not in calls


def test_sweep_separates_candidates_transport_and_consequence_transactions() -> None:
    source = OWNER_PATH.read_text(encoding="utf-8")
    sweep = _function("reconcile_pending_topups")
    sweep_calls = _attribute_calls(sweep) | _name_calls(sweep)
    claim_calls = [
        child
        for child in ast.walk(sweep)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "claim_topup_reconciliation_attempt"
    ]
    provider_calls = [
        child
        for child in ast.walk(sweep)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "observe_verification"
    ]

    assert "release_read_transaction" in sweep_calls
    assert "claim_topup_reconciliation_attempt" in sweep_calls
    assert "observe_verification" in sweep_calls
    assert "settle_verified_reconciled_topup" in sweep_calls
    assert "record_reconciled_gateway_observation" in sweep_calls
    assert len(claim_calls) == 1
    assert len(provider_calls) == 1
    assert claim_calls[0].lineno < provider_calls[0].lineno
    assert "SUPPORTED_PROVIDER_TYPES" in source
    assert "topup_reconciliation_batch_size" in source
    assert "topup_reconciliation_terminal_retry_hours" in source
    assert "checked_pending" in source
    assert "checked_terminal" in source
    terminal_statuses = next(
        node.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_TERMINAL_RECOVERY_STATUSES"
            for target in node.targets
        )
    )
    assert isinstance(terminal_statuses, ast.Tuple)
    assert {
        "TopupIntentStatus.failed",
        "TopupIntentStatus.abandoned",
        "TopupIntentStatus.canceled",
        "TopupIntentStatus.expired",
    } <= {ast.unparse(element) for element in terminal_statuses.elts}
    assert "_GATEWAY_PROVIDERS" not in source
    assert "_NOT_FOUND_STATUSES" not in source


def test_reconciliation_contract_declares_mutual_lane_and_provider_fairness() -> None:
    service = sot_relationships.service_relationship("financial.payment_reconciliation")

    assert service.contract is not None
    policy = " ".join(
        (
            service.notes,
            service.contract.transaction.boundary,
            service.contract.transaction.retries,
        )
    ).lower()
    assert "reserved capacity" in policy
    assert "pending" in policy
    assert "terminal" in policy
    assert "unused capacity" in policy
    assert "provider" in policy
    assert "interleave" in policy
    assert "least recently served" in policy
    assert "rotates provider priority" in policy
    assert "before provider i/o" in policy


def test_candidate_order_has_stable_attempt_created_and_id_tie_breakers() -> None:
    source = OWNER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(OWNER_PATH))
    order_by_calls = [
        child
        for child in ast.walk(tree)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "order_by"
    ]

    assert any(
        "gateway_last_reconcile_attempt_at" in ast.unparse(call)
        and "created_at" in ast.unparse(call)
        and "TopupIntent.id" in ast.unparse(call)
        for call in order_by_calls
    )


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
