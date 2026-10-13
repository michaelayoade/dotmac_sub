"""Keep the readiness owner and its adapter outside every write path."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/billing_numbering_readiness.py"
ADAPTER = ROOT / "scripts/migration/billing_numbering_readiness.py"
FORBIDDEN = {
    "configure_series",
    "allocate",
    "advance_counter",
    "advance_to_at_least",
    "reconcile_next_value",
    "lock_sequence",
    "generate_required_number",
    "generate_number",
    "generate_number_with_config",
    "commit",
    "rollback",
    "flush",
    "add",
    "add_all",
    "delete",
    "begin",
    "begin_nested",
    "execute_owner_command",
}


def violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lstrip()
            .upper()
            .startswith(
                (
                    "INSERT ",
                    "UPDATE ",
                    "DELETE ",
                    "ALTER ",
                    "CREATE ",
                    "DROP ",
                    "TRUNCATE ",
                )
            )
        ):
            found.append("write_sql")
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if name in FORBIDDEN:
                found.append(name)
        if isinstance(node, ast.ImportFrom):
            found.extend(alias.name for alias in node.names if alias.name in FORBIDDEN)
    return tuple(found)


def test_owner_and_adapter_cannot_allocate_or_complete_transactions() -> None:
    for path in (OWNER, ADAPTER):
        assert not violations(path.read_text())
    assert "install_settings_cache" not in OWNER.read_text()


def test_planted_writer_and_commit_are_detected() -> None:
    assert violations("from dotmac_numbering.service import allocate\ndb.commit()") == (
        "allocate",
        "commit",
    )


def test_adapter_delegates_to_one_owner_and_restores_cache() -> None:
    source = ADAPTER.read_text()
    assert "billing_numbering_readiness_report" in source
    assert "read_only_snapshot_session" in source
    assert "active_settings_cache" in source and "finally:" in source
    assert "NumberSeries" not in source and "select(" not in source


EXPECTED_CALLERS = {
    ("billing/invoices.py", "Invoices.stage_admin_draft"),
    ("billing/invoices.py", "Invoices.stage_system_invoice"),
    ("billing/invoices.py", "Invoices.create"),
    ("billing/invoices.py", "Invoices.create_for_subscription"),
    ("billing_automation.py", "run_invoice_cycle"),
    ("billing_automation.py", "generate_prorated_invoice"),
    ("crm_api.py", "create_installation_invoice"),
    ("invoice_draft_authoring.py", "_convert_proforma_invoice"),
    ("billing/credit_notes.py", "CreditNotes.create"),
    ("billing/credit_notes.py", "CreditNotes.issue_with_evidence"),
}


def numbering_callers(source: str) -> set[str]:
    class Inventory(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.callers: set[str] = set()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_Call(self, node: ast.Call) -> None:
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            direct = name == "generate_required_number" and any(
                isinstance(argument, ast.Constant)
                and argument.value in ("invoice_number", "credit_note_number")
                for argument in node.args
            )
            if name == "next_invoice_number" or direct:
                owner = ".".join(self.scope)
                if owner != "next_invoice_number":
                    self.callers.add(owner)
            self.generic_visit(node)

    inventory = Inventory()
    inventory.visit(ast.parse(source))
    return inventory.callers


def test_runtime_numbering_inventory_cannot_grow_silently() -> None:
    services = ROOT / "app/services"
    actual = {
        (str(path.relative_to(services)), owner)
        for path in services.rglob("*.py")
        for owner in numbering_callers(path.read_text())
    }
    assert actual == EXPECTED_CALLERS


def test_inventory_detects_planted_parallel_numbering_caller() -> None:
    assert numbering_callers(
        "def rogue(db):\n    return next_invoice_number(db)\n"
    ) == {"rogue"}


def test_planted_counter_advance_and_write_sql_are_detected() -> None:
    assert "advance_to_at_least" in violations("module.advance_to_at_least(db)")
    assert "write_sql" in violations(
        'db.execute(text("UPDATE document_sequences SET next_value=1"))'
    )
