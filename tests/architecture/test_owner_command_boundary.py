"""Keep the first migrated owner behind the standard command transaction API."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "device_projection_reconcile.py"
TASK = PROJECT_ROOT / "app" / "tasks" / "device_projection.py"


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def test_projection_owner_uses_one_public_transaction_boundary() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "UnitOfWork" not in source


def test_projection_task_only_owns_session_lifecycle() -> None:
    source = TASK.read_text(encoding="utf-8")
    calls = _calls(TASK)

    assert "owner_command_session" in calls
    assert "execute_owner_command" not in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_existing_owner_boundary_has_no_kernel_runtime_authority() -> None:
    boundary = PROJECT_ROOT / "app" / "services" / "owner_commands.py"
    tree = ast.parse(boundary.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(name.startswith("dotmac_kernel") for name in imports)


def test_optional_rollback_authorization_cannot_grant_commit_authority() -> None:
    boundary = PROJECT_ROOT / "app" / "services" / "owner_commands.py"
    tree = ast.parse(boundary.read_text(encoding="utf-8"))
    guard = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_reject_helper_commit"
    )
    names = {node.id for node in ast.walk(guard) if isinstance(node, ast.Name)}
    assert "_AUTHORIZED_SAVEPOINT_KEY" not in names
    assert "_OWNER_ROOT_KEY" in names
