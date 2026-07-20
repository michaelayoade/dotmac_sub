"""Keep reseller onboarding behind its contracted coordinator boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "reseller_onboarding.py"
ADAPTER = PROJECT_ROOT / "app" / "services" / "web_admin_resellers.py"
ROUTES = PROJECT_ROOT / "app" / "web" / "admin" / "resellers.py"
HANDLER = (
    PROJECT_ROOT / "app" / "services" / "events" / "handlers" / "reseller_invite.py"
)


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


def test_reseller_owner_uses_verified_boundary_without_helper_completion() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "web_system_user_mutations" not in source
    assert "send_user_invite" not in source


def test_reseller_admin_adapters_delegate_onboarding_without_persistence() -> None:
    service_source = ADAPTER.read_text(encoding="utf-8")
    route_calls = _calls(ROUTES)

    assert "reseller_onboarding.create_reseller" in service_source
    assert "reseller_onboarding.provision_reseller_user" in service_source
    assert "send_user_invite" not in service_source
    assert "create_reseller_user_principal" not in service_source
    assert not {"add", "delete", "flush", "commit"} & route_calls


def test_reseller_invite_handler_delegates_to_communication_owner() -> None:
    source = HANDLER.read_text(encoding="utf-8")
    calls = _calls(HANDLER)

    assert "submit_communication_intent" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls
    assert "send_email" not in source
