"""Keep collections grace precedence and timing behind one typed owner."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "collections" / "grace_policy.py"
SETTINGS = PROJECT_ROOT / "app" / "services" / "settings_spec.py"


def test_grace_policy_has_a_complete_read_only_manifest() -> None:
    service = next(
        item for item in all_services() if item.name == "financial.grace_policy"
    )

    assert service.is_contracted
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "read_only"
    assert service.contract.migration.state.value == "complete"
    assert {concern.name for concern in service.contract.concerns} == set(service.owns)
    assert service.contract.errors.domain_codes
    assert service.contract.errors.fail_closed_on


def test_owner_exposes_typed_provenance_phase_and_domain_failures() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "class GracePolicySource(StrEnum)" in source
    assert "class GracePolicySetSource(StrEnum)" in source
    assert "class GracePhase(StrEnum)" in source
    assert "class ResolvedPolicySet:" in source
    assert "class GracePolicyError(DomainError)" in source
    assert "source: GracePolicySource" in source
    assert "phase: GracePhase" in source
    assert "dict[str, Any]" not in source
    assert "asdict(" not in source
    assert "raise ValueError" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_default_policy_ids_use_the_settings_owner_not_raw_rows() -> None:
    owner_source = OWNER.read_text(encoding="utf-8")
    settings_source = SETTINGS.read_text(encoding="utf-8")

    assert "DomainSetting" not in owner_source
    assert "settings_spec.resolve_value(" in owner_source
    assert 'key="default_prepaid_policy_set_id"' in settings_source
    assert 'key="default_postpaid_policy_set_id"' in settings_source


def test_invalid_grace_evidence_cannot_be_clamped_to_immediate_action() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "max(0, int(account.grace_period_days))" not in source
    assert "max(0, int(policy.grace_days))" not in source
    assert "except (TypeError, ValueError):\n        days = 0" not in source
    assert 'code="financial.grace_policy.invalid_grace_days"' in source
