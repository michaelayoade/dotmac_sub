"""Pin provider settlement composition to named flush-only participants."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    source_path = ROOT / path
    return ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))


def _method(path: str, class_name: str, method_name: str) -> ast.FunctionDef:
    owner = next(
        node
        for node in _tree(path).body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


def _function(path: str, function_name: str) -> ast.FunctionDef:
    return next(
        node
        for node in _tree(path).body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
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


def _argument_names(node: ast.FunctionDef) -> set[str]:
    return {argument.arg for argument in (*node.args.args, *node.args.kwonlyargs)}


def test_settlement_participants_do_not_complete_the_caller_transaction() -> None:
    participants = (
        _method("app/services/billing/payments.py", "Payments", "stage_create"),
        _method(
            "app/services/billing/payments.py",
            "Payments",
            "stage_provider_fee_observation",
        ),
        _method(
            "app/services/billing/payments.py",
            "Payments",
            "stage_verified_provider_settlement",
        ),
        _method(
            "app/services/billing/payments.py",
            "Payments",
            "stage_status_transition",
        ),
        _method(
            "app/services/billing/payments.py",
            "PaymentAllocations",
            "stage_confirm",
        ),
        _method(
            "app/services/billing/payments.py",
            "PaymentAllocations",
            "stage_record_intent",
        ),
        _method(
            "app/services/billing/payments.py",
            "PaymentAllocationReconciliationExceptions",
            "stage_record",
        ),
        _method(
            "app/services/billing/payments.py",
            "PaymentAllocationReconciliationExceptions",
            "stage_resolve",
        ),
        _method(
            "app/services/billing/payments.py",
            "Refunds",
            "stage_provider_event_refund",
        ),
        _method(
            "app/services/billing/payments.py",
            "PaymentReversals",
            "stage_provider_event_reversal",
        ),
        _method(
            "app/services/billing/consolidated_payments.py",
            "ConsolidatedPaymentSettlements",
            "stage_settle_verified",
        ),
        _method(
            "app/services/billing/consolidated_payments.py",
            "ConsolidatedPaymentSettlements",
            "stage_confirm",
        ),
        _method(
            "app/services/billing/consolidated_payments.py",
            "ConsolidatedPaymentRefunds",
            "stage_provider_event",
        ),
        _method(
            "app/services/billing/consolidated_payments.py",
            "ConsolidatedPaymentReversals",
            "stage_provider_event",
        ),
        _method(
            "app/services/payment_provider_events.py",
            "PaymentProviderEvents",
            "stage_verified_webhook_event",
        ),
        _method(
            "app/services/payment_provider_events.py",
            "PaymentProviderEvents",
            "stage_verified_reconciliation_event",
        ),
        _function(
            "app/services/provider_payment_settlements.py",
            "stage_verified_invoice_payment",
        ),
    )

    for participant in participants:
        assert "commit" not in _argument_names(participant)
        assert "rollback" not in _argument_names(participant)
        calls = _attribute_calls(participant)
        assert "commit" not in calls
        assert "rollback" not in calls


def test_provider_event_composes_named_financial_participants() -> None:
    stage = _function(
        "app/services/payment_provider_events.py",
        "_stage_financial_consequences",
    )
    calls = _attribute_calls(stage)

    assert {
        "stage_create",
        "stage_provider_fee_observation",
        "stage_confirm",
        "stage_record_intent",
        "stage_settle_verified",
        "stage_status_transition",
        "stage_provider_event_refund",
        "stage_provider_event_reversal",
        "stage_provider_event",
    } <= calls

    adapter = (ROOT / "app/services/api_billing_webhooks.py").read_text(
        encoding="utf-8"
    )
    assert "db.get(Payment" not in adapter
    assert "pay.provider_fee =" not in adapter


def test_invoice_settlement_isolates_optional_allocation_consequence() -> None:
    implementation = _function(
        "app/services/provider_payment_settlements.py",
        "_settle_verified_invoice_payment",
    )
    record_exception = _function(
        "app/services/provider_payment_settlements.py",
        "_record_allocation_exception",
    )
    resolve_exception = _function(
        "app/services/provider_payment_settlements.py",
        "_resolve_allocation_exception",
    )
    calls = _attribute_calls(implementation)

    assert "stage_verified_provider_settlement" in calls
    assert "execute_owner_savepoint" in _name_calls(implementation)
    assert "stage_confirm" in calls
    assert "stage_record" in _attribute_calls(record_exception)
    assert "stage_resolve" in _attribute_calls(resolve_exception)
