"""Keep referral account conversion behind its contracted coordinator boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "referral_account_conversion.py"
SUBSCRIBER_OWNER = PROJECT_ROOT / "app" / "services" / "subscriber.py"
REFERRAL_OWNER = PROJECT_ROOT / "app" / "services" / "referrals.py"
API_ADAPTER = PROJECT_ROOT / "app" / "api" / "crm_referrals.py"
WEB_ADAPTER = PROJECT_ROOT / "app" / "web" / "admin" / "crm_referrals.py"
SETTINGS = PROJECT_ROOT / "app" / "services" / "settings_spec.py"


def _calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.append(node.func.attr)
    return calls


def test_conversion_owner_uses_one_verified_transport_neutral_boundary() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert calls.count("execute_owner_command") == 1
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "begin_nested" not in calls
    assert "HTTPException" not in source
    assert "status_code" not in source


def test_conversion_uses_transaction_neutral_record_owner_collaborators() -> None:
    owner = OWNER.read_text(encoding="utf-8")
    subscriber = SUBSCRIBER_OWNER.read_text(encoding="utf-8")
    referral = REFERRAL_OWNER.read_text(encoding="utf-8")

    assert "prepare_new_account" in owner
    assert "stage_prepared_account_created_event" in owner
    assert "attach_subscriber_for_conversion" in owner
    assert "def stage_prepared_account_created_event" in subscriber
    assert "def attach_subscriber_for_conversion" in referral
    assert "commit_prepared_account" not in owner
    assert "attach_subscriber(" not in owner


def test_public_capability_policy_has_one_bounded_runtime_source() -> None:
    owner = OWNER.read_text(encoding="utf-8")
    settings = SETTINGS.read_text(encoding="utf-8")

    assert "resolve_value" in _calls(OWNER)
    assert '"referral_signup_context_expiry_minutes"' in owner
    assert 'key="referral_signup_context_expiry_minutes"' in settings
    assert "_PUBLIC_CONTEXT_TTL" not in owner
    assert "timedelta(hours=24)" not in owner
    assert "min_value=5" in settings
    assert "max_value=10080" in settings


def test_all_conversion_adapters_construct_typed_commands() -> None:
    api = API_ADAPTER.read_text(encoding="utf-8")
    web = WEB_ADAPTER.read_text(encoding="utf-8")
    combined = f"{api}\n{web}"

    assert combined.count("AttachExistingReferralAccountCommand(") == 2
    assert combined.count("CreateReferralAccountCommand(") == 1
    assert combined.count("CreatePublicReferralAccountCommand(") == 1
    # The same adapters also host referrals.program commands; every conversion
    # path must still release any adapter read transaction before its owner.
    assert combined.count("release_read_transaction(db)") >= 4
    assert ".commit(" not in combined
    assert ".rollback(" not in combined

    after_conversion = api.split(
        "account = referral_account_conversion.create_public_account(", maxsplit=1
    )[1]
    before_enrollment = after_conversion.split(
        "enrollment = customer_credential_enrollment.request_referral_enrollment(",
        maxsplit=1,
    )[0]
    assert "release_read_transaction" not in before_enrollment


def test_conversion_stages_versioned_pii_free_owner_evidence() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "EventType.referral_account_converted" in source
    assert "stage_audit_event" in _calls(OWNER)
    assert '"schema_version": 1' in source
    assert '"command_id"' in source
    assert '"correlation_id"' in source
    assert 'action="referrals.account_converted"' in source
