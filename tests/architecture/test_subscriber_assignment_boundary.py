"""Keep subscriber access writes behind their contracted command owner."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "subscriber_assignments.py"
LEGACY = PROJECT_ROOT / "app" / "services" / "rbac.py"
API_ADAPTER = PROJECT_ROOT / "app" / "api" / "rbac.py"
RESELLER_COORDINATOR = PROJECT_ROOT / "app" / "services" / "reseller_onboarding.py"
SEED = PROJECT_ROOT / "scripts" / "seed" / "seed_rbac.py"


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


def _function_calls(path: Path, names: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in names:
            continue
        for statement in node.body:
            for child in ast.walk(statement):
                if isinstance(child, ast.Call) and isinstance(
                    child.func, ast.Attribute
                ):
                    calls.add(child.func.attr)
    return calls


def test_assignment_owner_uses_one_verified_transaction_boundary() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "SubscriberRole(" in source
    assert "SubscriberPermission(" in source


def test_legacy_assignment_service_is_retired() -> None:
    assert not LEGACY.exists()


def test_api_delegates_subscriber_role_commands_without_persistence() -> None:
    calls = _function_calls(
        API_ADAPTER,
        {
            "create_subscriber_role",
            "update_subscriber_role",
            "delete_subscriber_role",
        },
    )

    assert {
        "grant_subscriber_role",
        "update_subscriber_role",
        "revoke_subscriber_role",
    } <= calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls


def test_reseller_and_seed_writers_use_owner_collaborators() -> None:
    reseller_source = RESELLER_COORDINATOR.read_text(encoding="utf-8")
    seed_source = SEED.read_text(encoding="utf-8")

    assert "subscriber_assignments.ensure_role_grant_in_transaction" in (
        reseller_source
    )
    assert "subscriber_assignments.ensure_seeded_role_grant" in seed_source
    assert "SubscriberRole(" not in reseller_source
    assert "SubscriberRole(" not in seed_source
    assert "SubscriberPermission(" not in reseller_source
    assert "SubscriberPermission(" not in seed_source


def test_no_other_application_or_script_constructs_assignment_rows() -> None:
    offenders: list[str] = []
    for root in (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            if path == OWNER:
                continue
            calls = _calls(path)
            if {"SubscriberRole", "SubscriberPermission"} & calls:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []
