"""Keep credential recovery behind its contracted owner and durable delivery."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "credential_recovery.py"
HANDLER = (
    PROJECT_ROOT / "app" / "services" / "events" / "handlers" / "password_recovery.py"
)
SESSION_PROJECTION_HANDLER = (
    PROJECT_ROOT
    / "app"
    / "services"
    / "events"
    / "handlers"
    / "credential_session_projection.py"
)
PRODUCTION_ADAPTERS = (
    PROJECT_ROOT / "app" / "api" / "auth_flow.py",
    PROJECT_ROOT / "app" / "services" / "web_auth.py",
    PROJECT_ROOT / "app" / "services" / "web_customer_auth.py",
    PROJECT_ROOT / "app" / "services" / "web_reseller_auth.py",
    PROJECT_ROOT / "app" / "services" / "web_customer_user_access.py",
    PROJECT_ROOT / "app" / "services" / "web_system_user_mutations.py",
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


def test_recovery_owner_uses_verified_boundary_without_transport_completion() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "HTTPException" not in source
    assert "send_password_reset_email" not in source
    assert "send_email(" not in source


def test_request_handler_persists_intent_without_minting_or_sending() -> None:
    source = HANDLER.read_text(encoding="utf-8")
    calls = _calls(HANDLER)

    assert "submit_communication_intent" in calls
    assert "resolve_exact_recovery_target" in calls
    assert "issue_exact_reset_capability" not in calls
    assert "sign_context_token" not in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls
    assert "send_email" not in source


def test_completion_handler_is_the_session_projection_repair_owner() -> None:
    source = SESSION_PROJECTION_HANDLER.read_text(encoding="utf-8")
    calls = _calls(SESSION_PROJECTION_HANDLER)

    assert "invalidate_principal_strict" in calls
    assert "revoke_customer_sessions_for_subscriber" in calls
    assert "revoke_reseller_sessions_for_subscriber" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls


def test_production_adapters_do_not_call_retired_auth_flow_recovery_paths() -> None:
    retired = (
        "forgot_password_flow",
        "request_password_reset",
        "request_principal_password_reset",
        "request_system_user_password_reset",
        "auth_flow_service.reset_password",
        "send_password_reset_email",
    )
    offenders: list[str] = []
    for path in PRODUCTION_ADAPTERS:
        source = path.read_text(encoding="utf-8")
        for marker in retired:
            if marker in source:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {marker}")
    assert not offenders, "retired credential recovery calls:\n  " + "\n  ".join(
        offenders
    )
