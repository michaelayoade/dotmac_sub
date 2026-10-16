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


def test_locked_population_is_same_owner_observation_with_database_verified_fence() -> (
    None
):
    tree = ast.parse(OWNER.read_text())
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    locked = functions["capture_locked_billing_opening_population"]
    assert not forbidden_calls(locked)
    calls = {
        node.func.id
        for node in ast.walk(locked)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"_population", "_cash_evidence", "_documentary_observations"} <= calls
    assert (
        not {
            "_capture",
            "_classify",
            "_classify_cash",
            "authority_cutover",
            "resolve_position_evidence_at",
        }
        & calls
    )
    preview_calls = {
        node.func.id
        for node in ast.walk(functions["preview_billing_opening_export"])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "_capture",
        "_population",
        "_cash_evidence",
        "_classify_cash",
    } <= preview_calls
    source = ast.get_source_segment(OWNER.read_text(), locked)
    assert source is not None
    for proof in (
        'settings[0] != "read committed"',
        'settings[1] != "off"',
        "pg_backend_pid()",
        "pg_locks",
        "AND granted",
        "pg_inherits",
        "to_regclass(:relation) = to_regclass(:qualified)",
        "relkind = 'r'",
        "ShareRowExclusiveLock",
        "ExclusiveLock",
        "AccessExclusiveLock",
        "db.in_nested_transaction()",
    ):
        assert proof in source
    assert "ShareLock" not in source
    assert "LOCK TABLE" not in source
    # The final query has no caller-supplied lock token or bypass switch.
    assert tuple(arg.arg for arg in locked.args.kwonlyargs) == ("query",)


def test_locked_population_binds_closed_model_inventory_before_any_read() -> None:
    tree = ast.parse(OWNER.read_text())
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    inventories = {
        node.target.id: {element.id for element in node.value.elts}
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id in {"_BASE_POPULATION_MODELS", "_CASH_POPULATION_MODELS"}
        and isinstance(node.value, ast.Tuple)
    }
    base = inventories["_BASE_POPULATION_MODELS"]
    cash = inventories["_CASH_POPULATION_MODELS"]
    assert base == {
        "Subscriber",
        "Invoice",
        "InvoiceLine",
        "Payment",
        "CreditNote",
        "LedgerEntry",
        "CustomerPostingGroup",
        "CustomerPositionEffect",
        "PaymentRefund",
        "PaymentReversal",
        "PrepaidFundingBaseline",
        "CustomerSubledgerOpeningPosition",
        "PaymentAllocation",
        "CreditNoteApplication",
        "PrepaidOpeningFundingConsumption",
    }
    assert cash == {
        "PaymentSettlement",
        "PaymentProviderEvent",
        "IntegrationInbox",
        "PaymentProof",
        "PaymentProofCorrection",
        "TopupIntent",
    }
    binding = ast.get_source_segment(
        OWNER.read_text(), functions["_locked_population_relations"]
    )
    assert binding is not None
    for proof in (
        'getattr(db.get_bind, "__func__", None) is not Session.get_bind',
        'getattr(db.connection, "__func__", None) is not Session.connection',
        'bind.get_execution_options().get("schema_translate_map")',
        'connection.get_execution_options().get("schema_translate_map")',
        'table.schema not in {None, "public"}',
        "db.get_bind(mapper=mapper) is not bind",
        "db.get_bind(clause=table) is not bind",
        'db.connection(bind_arguments={"mapper": mapper}) is not connection',
        'db.connection(bind_arguments={"clause": table}) is not connection',
        "connection.get_transaction() is not transaction",
        "connection.in_nested_transaction()",
    ):
        assert proof in binding
    locked = ast.get_source_segment(
        OWNER.read_text(), functions["capture_locked_billing_opening_population"]
    )
    assert locked is not None
    assert locked.index("_locked_population_relations(") < locked.index("db.execute(")
    assert not forbidden_calls(functions["_locked_population_relations"])
    # Canonical settlement helper joins and aggregates must remain in the fence.
    helper = ast.parse((ROOT / "app/services/billing/_common.py").read_text())
    settlement = next(
        node
        for node in helper.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "resolve_invoice_settlement_amounts"
    )
    helper_models = {
        node.value.id
        for node in ast.walk(settlement)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id[0].isupper()
        and node.value.id != "PaymentStatus"
    }
    assert helper_models <= base
    imported_models = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("app.models.")
        for alias in node.names
    } - {"InvoiceStatus", "InvoiceDueDateBasis", "BillingRecordAuthority"}
    collector_models = {
        node.id
        for name in (
            "_population",
            "_sources",
            "_settlements",
            "_cash_evidence",
            "_documentary_observations",
        )
        for node in ast.walk(functions[name])
        if isinstance(node, ast.Name) and node.id in imported_models
    }
    assert collector_models == base | cash
    # Authority/position resolution belongs solely to preview classification.
    for name in (
        "_population",
        "_sources",
        "_settlements",
        "_cash_evidence",
        "_documentary_observations",
    ):
        assert not {
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        } & {"authority_cutover", "resolve_position_evidence_at"}
