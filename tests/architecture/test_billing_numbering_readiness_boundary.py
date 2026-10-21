"""Keep the readiness owner and its adapter outside every write path."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

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
        assert not violations(path.read_text(encoding="utf-8"))
    assert "install_settings_cache" not in OWNER.read_text(encoding="utf-8")


def test_planted_writer_and_commit_are_detected() -> None:
    assert violations("from dotmac_numbering.service import allocate\ndb.commit()") == (
        "allocate",
        "commit",
    )


def test_adapter_delegates_to_one_owner_and_restores_cache() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    assert "billing_numbering_readiness_report" in source
    assert "read_only_snapshot_session" in source
    assert "active_settings_cache" in source and "finally:" in source
    assert "NumberSeries" not in source and "select(" not in source


EXPECTED_ORDINARY_CALLERS = {
    ("billing/invoices.py", "Invoices.stage_admin_draft"),
    ("billing/invoices.py", "Invoices.stage_system_invoice"),
    ("billing/invoices.py", "Invoices.create"),
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

        def visit_FunctionDef(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            financial_series = ("invoice_number", "credit_note_number")
            direct = name == "generate_required_number" and any(
                isinstance(argument, ast.Constant)
                and argument.value in financial_series
                for argument in node.args
            )
            sequence_argument = {
                "generate_number": 2,
                "generate_number_with_config": 1,
            }.get(name)
            if sequence_argument is not None:
                positional = (
                    node.args[sequence_argument]
                    if len(node.args) > sequence_argument
                    else None
                )
                keyword = next(
                    (
                        item.value
                        for item in node.keywords
                        if item.arg == "sequence_key"
                    ),
                    None,
                )
                direct = direct or any(
                    isinstance(argument, ast.Constant)
                    and argument.value in financial_series
                    for argument in (positional, keyword)
                )
            if name == "next_invoice_number" or direct:
                owner = ".".join(self.scope)
                if owner != "next_invoice_number":
                    self.callers.add(owner)
            self.generic_visit(node)

    inventory = Inventory()
    inventory.visit(ast.parse(source))
    return inventory.callers


def function_definitions(source: str) -> tuple[tuple[str, int], ...]:
    """Keep every lexical function definition, including repeated definitions."""

    class Definitions(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.definitions: list[tuple[str, int]] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            self.scope.append(node.name)
            self.definitions.append((".".join(self.scope), node.lineno))
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    definitions = Definitions()
    definitions.visit(ast.parse(source))
    return tuple(definitions.definitions)


def inventory_errors(
    sources: Iterable[tuple[str, str]],
    ordinary_callers: set[tuple[str, str]],
    *,
    installation_allocates: bool = True,
    installation_module: str | None = None,
) -> tuple[str, ...]:
    actual: set[tuple[str, str]] = set()
    definitions: list[tuple[str, str, int]] = []
    for path, source in sources:
        actual.update((path, owner) for owner in numbering_callers(source))
        definitions.extend(
            (path, owner, occurrence)
            for owner, occurrence in function_definitions(source)
        )
    helper = "create_installation_invoice"
    matches = [
        (path, owner, line) for path, owner, line in definitions if owner == helper
    ]
    if len(matches) != 1:
        return (f"Expected one top-level {helper} definition; found {matches!r}",)
    path, owner, _ = matches[0]
    if installation_module is not None and path != installation_module:
        return (f"Expected {helper} in {installation_module}; found {path}",)
    expected = ordinary_callers | ({(path, owner)} if installation_allocates else set())
    if actual != expected:
        return (
            f"Installation helper discovered at {path}:{owner}; "
            f"missing callers={sorted(expected - actual)!r}; "
            f"unexpected callers={sorted(actual - expected)!r}",
        )
    return ()


def test_runtime_numbering_inventory_cannot_grow_silently() -> None:
    services = ROOT / "app/services"
    errors = inventory_errors(
        (
            (path.relative_to(services).as_posix(), path.read_text(encoding="utf-8"))
            for path in sorted(services.rglob("*.py"))
        ),
        EXPECTED_ORDINARY_CALLERS,
        installation_allocates=False,
    )
    assert not errors, errors


@pytest.mark.parametrize("declaration", ["def", "async def"])
def test_inventory_discovers_unique_top_level_helper(declaration: str) -> None:
    source = (
        f"{declaration} create_installation_invoice(db):\n"
        "    return next_invoice_number(db)\n"
    )
    assert not inventory_errors([("installation.py", source)], set())


@pytest.mark.parametrize(
    ("source", "extra_sources", "diagnostic"),
    [
        (
            "def create_installation_invoice(db):\n"
            "    return next_invoice_number(db)\n"
            "def rogue(db):\n    return next_invoice_number(db)\n",
            (),
            "unexpected callers=[('installation.py', 'rogue')]",
        ),
        (
            "def create_installation_invoice(db):\n    return None\n",
            (),
            "missing callers=[('installation.py', 'create_installation_invoice')]",
        ),
        (
            "def create_installation_invoice(db):\n"
            "    return next_invoice_number(db)\n"
            "def create_installation_invoice(db):\n    return None\n",
            (),
            "Expected one top-level",
        ),
        (
            "def create_installation_invoice(db):\n"
            "    return next_invoice_number(db)\n",
            (("duplicate.py", "def create_installation_invoice(db):\n    pass\n"),),
            "Expected one top-level",
        ),
        (
            "class Substitute:\n"
            "    def create_installation_invoice(db):\n"
            "        return next_invoice_number(db)\n",
            (),
            "Expected one top-level",
        ),
        (
            "def substitute():\n"
            "    async def create_installation_invoice(db):\n"
            "        return next_invoice_number(db)\n",
            (),
            "Expected one top-level",
        ),
    ],
)
def test_inventory_refuses_planted_caller_or_helper_drift(
    source: str,
    extra_sources: tuple[tuple[str, str], ...],
    diagnostic: str,
) -> None:
    errors = inventory_errors([("installation.py", source), *extra_sources], set())
    assert errors and diagnostic in errors[0]
    assert "installation.py" in errors[0] or "found []" in errors[0]


def test_definition_inventory_retains_occurrences_and_lexical_scopes() -> None:
    assert function_definitions(
        "def repeated():\n    pass\n"
        "async def repeated():\n    pass\n"
        "class Scope:\n"
        "    async def method():\n"
        "        def nested():\n            pass\n"
    ) == (
        ("repeated", 1),
        ("repeated", 3),
        ("Scope.method", 6),
        ("Scope.method.nested", 7),
    )


def test_inventory_detects_planted_parallel_numbering_caller() -> None:
    assert numbering_callers(
        "def rogue(db):\n    return next_invoice_number(db)\n"
    ) == {"rogue"}


@pytest.mark.parametrize(
    "call",
    [
        "generate_number(db, domain, {series}, enabled, prefix, padding, start)",
        "generate_number(db, domain, sequence_key={series}, enabled_key=enabled, prefix_key=prefix, padding_key=padding, start_key=start)",
        "generate_number_with_config(db, {series}, prefix=None, padding=None, start_value=None)",
        "generate_number_with_config(db, prefix=None, padding=None, start_value=None, sequence_key={series})",
    ],
)
@pytest.mark.parametrize("series", ["invoice_number", "credit_note_number"])
def test_inventory_detects_planted_direct_financial_numbering_call(
    call: str, series: str
) -> None:
    source = (
        "def create_installation_invoice(db):\n    return next_invoice_number(db)\n"
        f"def rogue(db):\n    return {call.format(series=repr(series))}\n"
    )
    errors = inventory_errors([("installation.py", source)], set())
    assert errors and "unexpected callers=[('installation.py', 'rogue')]" in errors[0]


def test_inventory_ignores_nonbilling_direct_numbering_series() -> None:
    source = (
        "def create_installation_invoice(db):\n    return next_invoice_number(db)\n"
        "def rogue(db):\n"
        "    return generate_number_with_config(db, 'payment_receipt', "
        "prefix=None, padding=None, start_value=None)\n"
    )
    assert numbering_callers(source) == {"create_installation_invoice"}


def test_planted_counter_advance_and_write_sql_are_detected() -> None:
    assert "advance_to_at_least" in violations("module.advance_to_at_least(db)")
    assert "write_sql" in violations(
        'db.execute(text("UPDATE document_sequences SET next_value=1"))'
    )


def test_invoice_routes_delegate_to_the_incumbent_numbering_owner() -> None:
    """The deferred cutover inventory covers every route, including conversion."""
    expected = EXPECTED_ORDINARY_CALLERS - {
        ("billing/credit_notes.py", "CreditNotes.create"),
        ("billing/credit_notes.py", "CreditNotes.issue_with_evidence"),
    }
    actual: set[tuple[str, str]] = set()
    services = ROOT / "app/services"
    for path in sorted(services.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(services).as_posix()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "generate_required_number":
                owner = next(
                    (
                        parent.name
                        for parent in ast.walk(tree)
                        if isinstance(parent, ast.FunctionDef)
                        and parent.name == "next_invoice_number"
                        and parent.lineno <= node.lineno <= parent.end_lineno
                    ),
                    None,
                )
                invoice_number_call = any(
                    isinstance(argument, ast.Constant)
                    and argument.value == "invoice_number"
                    for argument in node.args
                )
                if invoice_number_call:
                    assert (relative, owner) == (
                        "billing/invoices.py",
                        "next_invoice_number",
                    ), (
                        f"Legacy invoice writer escaped its owner: {relative}:{node.lineno}"
                    )
            if name == "next_invoice_number":
                assert len(node.args) == 1, (relative, node.lineno)
                assert not node.keywords, (relative, node.lineno)
        actual.update(
            (relative, owner)
            for owner in numbering_callers(source)
            if owner not in {"CreditNotes.create", "CreditNotes.issue_with_evidence"}
        )
    assert actual == expected
    assert len(actual) == 4


def test_invoice_number_owner_uses_only_the_legacy_numbering_boundary() -> None:
    tree = ast.parse(
        (ROOT / "app/services/billing/invoices.py").read_text(encoding="utf-8")
    )
    participant = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "next_invoice_number"
    )
    calls = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(participant)
        if isinstance(node, ast.Call)
    }
    assert "generate_required_number" in calls
    assert "allocate" not in calls
    assert not calls.intersection(
        {
            "generate_number",
            "configure_series",
            "advance_to_at_least",
            "commit",
            "rollback",
            "begin",
            "begin_nested",
            "execute_owner_command",
        }
    )


def test_upstream_invoice_routes_delegate_without_preallocation() -> None:
    routes = {
        ("billing/invoices.py", "create_for_subscription"): "create_with_lines",
        ("billing_automation.py", "run_invoice_cycle"): "stage_system_invoice",
        ("billing_automation.py", "generate_prorated_invoice"): "stage_system_invoice",
    }
    for (relative, function), constructor in routes.items():
        tree = ast.parse((ROOT / "app/services" / relative).read_text(encoding="utf-8"))
        owners = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function
        ]
        assert len(owners) == 1, (relative, function)
        calls = {
            getattr(node.func, "attr", getattr(node.func, "id", ""))
            for node in ast.walk(owners[0])
            if isinstance(node, ast.Call)
        }
        assert constructor in calls, (relative, function)
        assert "next_invoice_number" not in calls

    services = ROOT / "app/services"
    installation_helpers = []
    for path in sorted(services.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == (
                "create_installation_invoice"
            ):
                installation_helpers.append(node)
    assert len(installation_helpers) == 1
    calls = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(installation_helpers[0])
        if isinstance(node, ast.Call)
    }
    assert "create_invoice_with_lines" in calls
    assert "next_invoice_number" not in calls


def test_create_with_lines_delegates_numbering_to_the_draft_owner() -> None:
    tree = ast.parse(
        (ROOT / "app/services/billing/invoices.py").read_text(encoding="utf-8")
    )
    owner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create_with_lines"
    )
    constructor = next(
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "stage_admin_draft"
    )
    assert not constructor.keywords


def test_reviewed_document_adoption_never_allocates_a_replacement_label() -> None:
    tree = ast.parse(
        (ROOT / "app/services/billing/invoices.py").read_text(encoding="utf-8")
    )
    methods = {
        "adopt_prepaid_proforma_document_for_owner",
        "adopt_reviewed_prepaid_draft_document_for_owner",
        "adopt_reviewed_prepaid_sequence_document_for_owner",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in methods:
            calls = {
                getattr(call.func, "attr", getattr(call.func, "id", ""))
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
            assert not calls.intersection(
                {"allocate", "next_invoice_number", "generate_required_number"}
            )
            methods.remove(node.name)
    assert not methods


def test_retired_installation_allocator_retains_unique_canonical_helper() -> None:
    source = "def create_installation_invoice(db):\n    return create_with_lines(db)\n"
    assert (
        inventory_errors(
            [("installation.py", source)],
            set(),
            installation_allocates=False,
            installation_module="installation.py",
        )
        == ()
    )
    for sources, diagnostic in (
        ([("installation.py", source + source)], "Expected one top-level"),
        (
            [("moved.py", source)],
            "Expected create_installation_invoice in installation.py",
        ),
        (
            [("installation.py", source), ("shadow.py", source)],
            "Expected one top-level",
        ),
    ):
        errors = inventory_errors(
            sources,
            set(),
            installation_allocates=False,
            installation_module="installation.py",
        )
        assert errors and diagnostic in errors[0]
