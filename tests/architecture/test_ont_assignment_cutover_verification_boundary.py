from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/ont_assignment_cutover_verification.py"
COMMAND = PROJECT_ROOT / "scripts/network/verify_ont_assignment_cutover_batch.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/342_ont_assignment_cutover_verification.py"


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


def _called_attributes(tree: ast.Module, receiver: str) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == receiver
    }


def test_verification_owner_cannot_execute_or_mutate_assignment_state():
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


def test_verification_cli_and_migration_add_no_apply_or_constraint_path():
    command_tree = _tree(COMMAND)
    migration_tree = _tree(MIGRATION)
    command_names = {
        node.args[0].value
        for node in ast.walk(command_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }

    assert command_names == {"preview", "attest", "inspect"}
    assert _called_attributes(migration_tree, "op").isdisjoint(
        {
            "add_column",
            "alter_column",
            "create_check_constraint",
            "create_foreign_key",
            "create_unique_constraint",
            "execute",
        }
    )
