"""Keep system-user access writes behind their contracted command owner."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "system_user_assignments.py"
STAFF_OWNER = PROJECT_ROOT / "app" / "services" / "staff_provisioning.py"
PROFILE_WRITER = PROJECT_ROOT / "app" / "services" / "web_system_user_edit.py"
LEGACY_MUTATIONS = PROJECT_ROOT / "app" / "services" / "web_system_user_mutations.py"
ADMIN_ADAPTER = PROJECT_ROOT / "app" / "web" / "admin" / "system.py"


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


def test_assignment_owner_uses_one_verified_transaction_boundary() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "SystemUserRole(" in source
    assert "SystemUserPermission(" in source


def test_legacy_application_writers_no_longer_construct_assignment_rows() -> None:
    for path in (PROFILE_WRITER, LEGACY_MUTATIONS):
        source = path.read_text(encoding="utf-8")
        assert "SystemUserRole(" not in source
        assert "SystemUserPermission(" not in source


def test_admin_access_adapter_delegates_without_persistence_calls() -> None:
    tree = ast.parse(ADMIN_ADAPTER.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "user_assignments_submit"
    )
    calls = {
        node.func.attr
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "replace_system_user_assignments" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls


def test_staff_owner_delegates_source_grants_and_admin_invariant() -> None:
    source = STAFF_OWNER.read_text(encoding="utf-8")

    assert "sync_source_roles_by_names" in source
    assert "sync_source_roles_by_ids" in source
    assert "ensure_can_deactivate_system_user" in source
    assert "SystemUserRole(" not in source
