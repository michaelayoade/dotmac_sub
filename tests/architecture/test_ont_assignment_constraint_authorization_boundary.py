from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = (
    PROJECT_ROOT / "app/services/network/ont_assignment_constraint_authorization.py"
)
COMMAND = (
    PROJECT_ROOT / "scripts/network/review_ont_assignment_constraint_authorization.py"
)
MIGRATION = (
    PROJECT_ROOT / "alembic/versions/343_ont_assignment_constraint_authorization.py"
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _subcommands(tree: ast.Module) -> set[str]:
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def test_authorization_owner_has_no_assignment_or_ddl_authority():
    tree = _tree(SERVICE)
    imported = _imported_names(tree)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "OntAssignment" not in imported
    assert "execute_assignment_identity_repair" not in imported
    assert "approve_assignment_identity_repair" not in imported
    assert "decline_assignment_identity_repair" not in imported
    assert "execute_assignment_identity_repair" not in calls
    assert "enable_constraint" not in calls
    assert "create_constraint" not in calls
    assert "execute" not in calls


def test_authorization_cli_has_evidence_modes_only():
    commands = _subcommands(_tree(COMMAND))

    assert commands == {
        "request-preview",
        "request",
        "review-preview",
        "review",
        "inspect",
    }
    assert commands.isdisjoint({"execute", "apply", "enable", "disable", "ddl"})


def test_authorization_migration_only_adds_evidence_tables_and_indexes():
    tree = _tree(MIGRATION)
    op_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]
    created_tables = {
        node.args[0].value
        for node in op_calls
        if node.func.attr == "create_table"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    assert created_tables == {
        "ont_assignment_constraint_authorization_requests",
        "ont_assignment_constraint_authorization_reviews",
    }
    assert {node.func.attr for node in op_calls}.isdisjoint(
        {
            "add_column",
            "alter_column",
            "create_check_constraint",
            "create_foreign_key",
            "execute",
        }
    )
