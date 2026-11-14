"""All ordinary invoice-line constructors remain behind the shared guard."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = "app/services/billing/invoices.py"


def test_invoice_line_constructor_inventory_is_owned_and_guarded() -> None:
    constructors: list[tuple[str, str]] = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            writes = [
                node
                for node in calls
                if isinstance(node.func, ast.Name) and node.func.id == "InvoiceLine"
            ]
            if not writes:
                continue
            relative = path.relative_to(ROOT).as_posix()
            constructors.extend((relative, function.name) for _ in writes)
            assert relative == OWNER, (
                "New line writer must enter the invoice owner's claim guard"
            )
            guards = [
                node
                for node in calls
                if isinstance(node.func, ast.Name)
                and node.func.id == "assert_invoice_period_available"
            ]
            locks = [
                node
                for node in calls
                if isinstance(node.func, ast.Name) and node.func.id == "lock_account"
            ]
            assert guards and locks
            assert min(node.lineno for node in locks) < min(
                node.lineno for node in guards
            )
            assert min(node.lineno for node in guards) < min(
                node.lineno for node in writes
            )
    assert sorted(constructors) == [
        (OWNER, "replace_admin_draft_lines"),
        (OWNER, "stage_system_line"),
    ]


def test_issuance_and_generic_update_keep_reservation_checks() -> None:
    tree = ast.parse((ROOT / OWNER).read_text(encoding="utf-8"))
    methods = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    issue = ast.unparse(methods["issue_draft_system"])
    assert "Invoices.assert_service_period_reservations_for_issue(db, invoice)" in issue
    recheck = ast.unparse(methods["assert_service_period_reservations_for_issue"])
    assert "Invoices.validate_service_period_reservations(db, invoice)" in recheck
    validation = ast.unparse(methods["validate_service_period_reservations"])
    assert "db.flush" not in validation
    assert "assert_invoice_period_available" in validation
    assert "exclude_invoice_id=invoice.id" in validation
    assert not (ROOT / "app/services/billing/invoice_period_reservations.py").exists()
    facts = ast.unparse(methods["invoice_period_conflict"])
    assert "resolve_service_charge_conflict_v1" in facts
    # Public incremental mutations are retired; aggregate and automation owners remain.
    update = ast.unparse(methods["update"])
    assert "catalogue_line_owner_required" in update
    assert "setattr(" not in update
