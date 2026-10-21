"""The invoice opening census observes evidence; it owns no alternate writer."""

import ast
from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/billing/opening_export.py"


def forbidden_calls(tree: ast.AST) -> tuple[str, ...]:
    session_methods = {
        "commit",
        "rollback",
        "flush",
        "add",
        "add_all",
        "delete",
        "begin",
        "begin_nested",
    }
    writer_calls = {
        "execute_owner_command",
        "execute_owner_savepoint",
        "stage_posting_group",
        "stage_reversal",
        "with_for_update",
        "Session",
        "sessionmaker",
        "create_engine",
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            if function.attr in writer_calls or (
                function.attr in session_methods
                and isinstance(function.value, ast.Name)
                and function.value.id == "db"
            ):
                found.append(function.attr)
        elif isinstance(function, ast.Name) and function.id in writer_calls:
            found.append(function.id)
    return tuple(found)


def test_query_has_no_write_or_adapter_dependency() -> None:
    tree = ast.parse(OWNER.read_text())
    assert not forbidden_calls(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                (
                    "fastapi",
                    "celery",
                    "app.web",
                    "app.api",
                    "app.services.owner_commands",
                    "app.services.collections",
                )
            )


def test_only_canonical_financial_resolvers_interpret_amounts() -> None:
    tree = ast.parse(OWNER.read_text())
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "resolve_invoice_settlement_amounts",
        "resolve_position_evidence_at",
        "authority_cutover",
    } <= calls
    text = OWNER.read_text()
    assert "metadata_" not in text
    assert "get_account_credit_balance" not in text
    assert "balance = max(" not in text
    assert "pg_current_snapshot()" in text
    assert "transaction_read_only" in text


def test_census_has_one_query_only_owner_without_invoice_mutation_claims() -> None:
    service = service_relationship("financial.invoices.opening_export")
    assert service.owns == ("complete billing opening evidence census",)
    contract = service.contract
    assert contract is not None
    assert contract.transaction.mode == TransactionMode.READ_ONLY
    assert {c.role for c in contract.concerns} == {OwnerRole.RESOLVER}
    assert contract.events is None
    assert service.module == "app.services.billing.opening_export"
    assert (
        "invoice document lifecycle" in service_relationship("financial.invoices").owns
    )


def test_write_guard_detects_session_add_without_rejecting_local_set_add() -> None:
    assert forbidden_calls(ast.parse("db.add(row)")) == ("add",)
    assert forbidden_calls(ast.parse("db.commit()")) == ("commit",)
    assert (
        forbidden_calls(ast.parse("seen_ids = set()\nseen_ids.add(identifier)")) == ()
    )
    assert forbidden_calls(ast.parse("execute_owner_command(db)")) == (
        "execute_owner_command",
    )


def test_documentary_observer_selects_only_current_persisted_invoice_columns() -> None:
    tree = ast.parse(OWNER.read_text())
    observer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_documentary_observations"
    )
    assert not forbidden_calls(observer)
    selected = {
        (argument.value.id, argument.attr)
        for node in ast.walk(observer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "select"
        for argument in node.args
        if isinstance(argument, ast.Attribute) and isinstance(argument.value, ast.Name)
    }
    assert selected == {
        ("Invoice", field)
        for field in (
            "id",
            "subtotal",
            "discount_amount",
            "tax_total",
            "total",
            "billing_period_start",
            "billing_period_end",
        )
    } | {
        ("InvoiceLine", field)
        for field in (
            "id",
            "invoice_id",
            "description",
            "tax_rate_id",
            "tax_rate_snapshot_version",
            "tax_rate_code_snapshot",
            "tax_rate_percent_snapshot",
            "tax_rate_is_active_snapshot",
            "tax_application",
        )
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                (
                    "dotmac_billing",
                    "app.services.billing_invoice_pdf",
                    "app.services.billing.withholding",
                    "app.services.billing.non_cash",
                )
            )
    assert "TaxRate" not in {
        node.id for node in ast.walk(observer) if isinstance(node, ast.Name)
    }
    assert "Subscriber" not in {
        node.id for node in ast.walk(observer) if isinstance(node, ast.Name)
    }
