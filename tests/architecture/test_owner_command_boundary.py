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


_KERNEL_TRANSACTION_OBSERVERS = frozenset(
    {
        "ConflictSavepointPhase",
        "claim_conflict_savepoint_commit",
        "current_conflict_savepoint",
        "conflict_savepoint_requires_rollback",
    }
)


def _kernel_boundary_violations(source: str) -> set[str]:
    """Permit only the public engine-free observation/claim import surface."""
    violations: set[str] = set()
    tree = ast.parse(source)
    aliases = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("dotmac_kernel") and (
                module != "dotmac_kernel.transactions"
                or node.level
                or not {alias.name for alias in node.names}
                <= _KERNEL_TRANSACTION_OBSERVERS
            ):
                violations.add(module)
        elif isinstance(node, ast.Import):
            violations.update(
                alias.name
                for alias in node.names
                if alias.name.startswith("dotmac_kernel")
            )
        elif isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            name = aliases.get(name, name)
            if name in {
                "Session",
                "create_engine",
                "create_async_engine",
                "sessionmaker",
                "async_sessionmaker",
                "scoped_session",
                "__import__",
                "import_module",
            }:
                violations.add(name)
    return violations


def test_existing_owner_boundary_has_only_public_kernel_observation_authority() -> None:
    boundary = PROJECT_ROOT / "app" / "services" / "owner_commands.py"
    source = boundary.read_text(encoding="utf-8")
    assert _kernel_boundary_violations(source) == set()
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "dotmac_kernel.transactions"
        for alias in node.names
    }
    assert imported == _KERNEL_TRANSACTION_OBSERVERS
    assert "_dotmac_kernel_" not in source


def test_kernel_boundary_import_guard_is_sensitive() -> None:
    assert (
        _kernel_boundary_violations(
            "from dotmac_kernel.transactions import "
            "ConflictSavepointPhase, claim_conflict_savepoint_commit, "
            "current_conflict_savepoint, conflict_savepoint_requires_rollback"
        )
        == set()
    )
    for source, rejected in (
        ("import dotmac_kernel.db", "dotmac_kernel.db"),
        ("from dotmac_kernel import db", "dotmac_kernel"),
        (
            "from dotmac_kernel._transactions import current_conflict_savepoint",
            "dotmac_kernel._transactions",
        ),
        (
            "from dotmac_kernel.session_runtime import SessionLocal",
            "dotmac_kernel.session_runtime",
        ),
        (
            "from dotmac_kernel.transactions import conflict_savepoint",
            "dotmac_kernel.transactions",
        ),
        ("from dotmac_kernel.transactions import *", "dotmac_kernel.transactions"),
        (
            "import dotmac_kernel.transactions as transactions",
            "dotmac_kernel.transactions",
        ),
        ("session = Session()", "Session"),
        (
            "from sqlalchemy.orm import Session as OwnedSession\nOwnedSession()",
            "Session",
        ),
        ("factory = orm.sessionmaker()", "sessionmaker"),
        ("engine = create_engine('unused')", "create_engine"),
        ("runtime = import_module('dotmac_kernel.db')", "import_module"),
    ):
        assert _kernel_boundary_violations(source) == {rejected}


def test_kernel_release_phase_cannot_grant_commit_authority() -> None:
    boundary = PROJECT_ROOT / "app" / "services" / "owner_commands.py"
    tree = ast.parse(boundary.read_text(encoding="utf-8"))
    guard = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_reject_helper_commit"
    )
    names = {node.id for node in ast.walk(guard) if isinstance(node, ast.Name)}
    assert "claim_conflict_savepoint_commit" in names
    assert "conflict_savepoint_requires_rollback" in names
    assert "current_conflict_savepoint" not in names
    assert "ConflictSavepointPhase" not in names


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
