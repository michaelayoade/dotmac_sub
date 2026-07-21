"""Keep billing-mode resolution and transition policy behind one typed owner."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "billing_profile.py"
CLEANUP = PROJECT_ROOT / "app" / "services" / "billing_cleanup_remediation.py"
GRACE_POLICY = PROJECT_ROOT / "app" / "services" / "collections" / "grace_policy.py"


def test_billing_profile_has_a_complete_read_only_manifest() -> None:
    service = next(
        item for item in all_services() if item.name == "financial.billing_profile"
    )

    assert service.is_contracted
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "read_only"
    assert service.contract.migration.state.value == "complete"
    assert {concern.name for concern in service.contract.concerns} == set(service.owns)
    assert service.contract.errors.domain_codes
    assert service.contract.errors.fail_closed_on


def test_owner_exposes_typed_outcomes_and_transport_neutral_errors() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "class BillingProfileSource(StrEnum)" in source
    assert "class BillingProfileReason(StrEnum)" in source
    assert "class BillingProfileError(DomainError)" in source
    assert "source: BillingProfileSource" in source
    assert "reason: BillingProfileReason | None" in source
    assert "HTTPException" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "class BillingModeWriteRejected(ValueError)" not in source


def test_cleanup_and_grace_callers_do_not_recreate_billing_profile_policy() -> None:
    cleanup_source = CLEANUP.read_text(encoding="utf-8")
    grace_source = GRACE_POLICY.read_text(encoding="utf-8")

    assert "plan_billing_mode_transition(" in cleanup_source
    assert cleanup_source.count("resolve_billing_profile(") >= 2
    assert "live_modes =" not in cleanup_source
    assert "require_effective_billing_mode(profile)" in grace_source
    assert "or account.billing_mode or BillingMode.prepaid" not in grace_source
