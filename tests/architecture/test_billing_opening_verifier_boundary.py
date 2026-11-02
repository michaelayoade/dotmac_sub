"""The Gap B opening verifier observes and verifies; it owns no writer or secret path."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/services/billing/opening_verifier.py"
CLI = ROOT / "scripts/migration/billing_opening_authority.py"
FORBIDDEN_NAMES = {"resolve_secret", "get_secret", "environ", "getenv"}
WRITE_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE|GRANT|REVOKE|COPY|"
    r"SET\s+ROLE|NOTIFY|LOCK|VACUUM)\b",
    re.IGNORECASE,
)


def _tree() -> ast.AST:
    return ast.parse(MODULE.read_text())


def _imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
    return names


def test_no_money_models_or_writer_services_imported() -> None:
    for name in _imports(_tree()):
        assert not name.startswith("app.models"), name
        assert "starter_" not in name and "payments" not in name, name
        assert not name.startswith(("fastapi", "celery", "app.web", "app.tasks")), name
    allowed_app = {
        "app.db",
        "app.services.secrets",
        "app.services.billing.opening_census_wire",
        "app.services.billing.opening_export",
        "app.services.billing.opening_export_contracts",
    }
    for name in _imports(_tree()):
        if name.startswith("app."):
            assert any(name == a or name.startswith(a + ".") for a in allowed_app), name


def test_no_plaintext_secret_resolution_or_environment_access() -> None:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, node.id
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_NAMES, node.attr
        if isinstance(node, ast.alias):
            assert node.name not in FORBIDDEN_NAMES, node.name
    # The only secret resolver permitted is the OpenBao pointer resolver.
    assert "resolve_openbao_ref" in MODULE.read_text()


def test_observation_port_contains_only_read_sql_and_no_session_writes() -> None:
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.upper().lstrip().startswith("SELECT"):
                assert not WRITE_SQL.search(node.value), node.value
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                "commit",
                "flush",
                "add",
                "add_all",
                "delete",
                "begin_nested",
                "with_for_update",
            }, node.func.attr


def test_only_the_cli_and_tests_import_the_module() -> None:
    offenders: list[str] = []
    for base in ("app", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            if path in {MODULE, CLI}:
                continue
            if "opening_verifier" in path.read_text(errors="ignore"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_adapter_is_executed_only_from_hash_pinned_bytes() -> None:
    text = MODULE.read_text()
    assert "spec_from_file_location" not in text
    assert "importlib" not in text
    assert "compile(raw," in text
