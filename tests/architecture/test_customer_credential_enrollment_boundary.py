"""Keep customer credential enrollment behind its contracted owner boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "customer_credential_enrollment.py"
PROJECTION_HANDLER = (
    PROJECT_ROOT
    / "app"
    / "services"
    / "events"
    / "handlers"
    / "credential_session_projection.py"
)
PRODUCTION_ADAPTERS = (
    PROJECT_ROOT / "app" / "api" / "crm_referrals.py",
    PROJECT_ROOT / "app" / "api" / "auth_flow.py",
    PROJECT_ROOT / "app" / "services" / "web_customer_auth.py",
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


def test_enrollment_owner_uses_verified_boundary_without_transport_completion() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "begin_nested" not in calls
    assert "HTTPException" not in source
    assert "status_code" not in source
    assert "invalidate_principal" not in calls
    assert "send_email" not in source


def test_enrollment_commands_stage_versioned_events_and_non_secret_delivery() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "EventType.customer_credential_enrollment_requested" in source
    assert "EventType.customer_credential_enrollment_completed" in source
    assert "dedupe_key=_request_dedupe_key" in source
    assert "body=None" in source
    assert "email_sha256" in source
    assert "subject=None" in source


def test_enrollment_runtime_policy_has_one_canonical_settings_source() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "resolve_value" in _calls(OWNER)
    assert "_TOKEN_TTL" not in source
    assert "_DELIVERY_LIMIT" not in source
    assert "_DELIVERY_WINDOW_SECONDS" not in source
    assert '"user_invite_expiry_minutes"' in source
    assert '"password_min_length"' in source
    assert '"credential_enrollment_request_limit"' in source
    assert '"credential_enrollment_request_window_seconds"' in source


def test_enrollment_completion_uses_strict_replayable_cache_projection() -> None:
    source = PROJECTION_HANDLER.read_text(encoding="utf-8")
    calls = _calls(PROJECTION_HANDLER)

    assert "EventType.customer_credential_enrollment_completed" in source
    assert "invalidate_principal_strict" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls


def test_production_adapters_construct_typed_enrollment_commands() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in PRODUCTION_ADAPTERS
    )

    assert "RequestReferralEnrollmentCommand(" in combined
    assert combined.count("CompleteReferralEnrollmentCommand(") == 2
    assert combined.count("except DomainError") >= 3
    assert "release_read_transaction(db)" in combined
