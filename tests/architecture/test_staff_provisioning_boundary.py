"""Keep staff lifecycle writes behind the contracted coordinator boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "staff_provisioning.py"
ADAPTER = PROJECT_ROOT / "app" / "api" / "staff_sync.py"
HANDLER = PROJECT_ROOT / "app" / "services" / "events" / "handlers" / "staff_invite.py"


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


def test_staff_owner_uses_verified_boundary_without_helper_completion() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "web_system_user_mutations" not in source
    assert "send_user_invite" not in source


def test_staff_sync_adapter_owns_no_persistence_or_delivery() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    calls = _calls(ADAPTER)

    assert "provision_staff_account" in calls
    assert "sync_staff_account_roles" in calls
    assert "set_staff_account_active" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls
    assert "send_user_invite" not in source


def test_staff_invite_handler_delegates_to_communication_owner() -> None:
    source = HANDLER.read_text(encoding="utf-8")
    calls = _calls(HANDLER)

    assert "submit_communication_intent" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls
    assert "send_email" not in source
