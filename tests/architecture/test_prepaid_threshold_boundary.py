"""Keep prepaid currency and threshold decisions behind typed policy owners."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import all_services

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CURRENCY_OWNER = PROJECT_ROOT / "app" / "services" / "prepaid_currency.py"
THRESHOLD_OWNER = PROJECT_ROOT / "app" / "services" / "prepaid_threshold.py"
COVERAGE_OWNER = PROJECT_ROOT / "app" / "services" / "prepaid_service_coverage.py"
COVERAGE_RECONCILER = (
    PROJECT_ROOT / "app" / "services" / "prepaid_coverage_reconciliation.py"
)
ACCESS_OWNER = PROJECT_ROOT / "app" / "services" / "access_resolution.py"
SERVICE_STATUS = PROJECT_ROOT / "app" / "services" / "service_status.py"


def _service(name: str):
    return next(item for item in all_services() if item.name == name)


def test_prepaid_currency_and_threshold_have_complete_read_only_manifests() -> None:
    currency = _service("financial.prepaid_currency")
    threshold = _service("financial.prepaid_threshold")

    for service in (currency, threshold):
        assert service.is_contracted
        assert service.contract is not None
        assert service.contract.transaction.mode.value == "read_only"
        assert service.contract.migration.state.value == "complete"
        assert {concern.name for concern in service.contract.concerns} == set(
            service.owns
        )
        assert service.contract.errors.domain_codes
        assert service.contract.errors.fail_closed_on


def test_prepaid_coverage_has_one_typed_read_only_owner() -> None:
    coverage = _service("financial.prepaid_service_coverage")
    source = COVERAGE_OWNER.read_text(encoding="utf-8")
    threshold_source = THRESHOLD_OWNER.read_text(encoding="utf-8")

    assert coverage.is_contracted
    assert coverage.contract is not None
    assert coverage.contract.transaction.mode.value == "read_only"
    assert coverage.contract.errors.fail_closed_on
    assert "class PrepaidServiceCoverageDecision:" in source
    assert "class PrepaidCoverageEvidence:" in source
    assert "resolve_prepaid_service_coverage(" in threshold_source
    assert "ServiceEntitlement" not in threshold_source
    assert "InvoiceLine" not in threshold_source
    assert "InvoiceLine" not in source
    assert "InvoiceStatus" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_prepaid_coverage_repair_has_one_typed_reconciler() -> None:
    reconciler = _service("financial.prepaid_service_coverage_reconciliation")
    source = COVERAGE_RECONCILER.read_text(encoding="utf-8")

    assert reconciler.is_contracted
    assert reconciler.contract is not None
    assert reconciler.contract.transaction.mode.value == "owner_managed"
    assert reconciler.contract.migration.state.value == "complete"
    assert "execute_owner_command(" in source
    assert "PrepaidCoverageReconciliationRun(" in source
    assert "PrepaidCoverageReconciliationItem(" in source
    assert "source_invoice_line_id=" in source
    assert "source_account_adjustment_id=" in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_threshold_exposes_typed_provenance_and_domain_failures() -> None:
    source = THRESHOLD_OWNER.read_text(encoding="utf-8")

    assert "class PrepaidThresholdDecision:" in source
    assert "class PrepaidThresholdError(DomainError)" in source
    assert "class PrepaidCurrencyMismatchError(PrepaidThresholdError)" in source
    assert "unresolved_renewal_subscription_ids" in source
    assert "resolve_prepaid_monthly_charges" in source
    assert "OfferPrice" not in source
    assert "OfferVersionPrice" not in source
    assert "Sequence[Any]" not in source
    assert "list[Any]" not in source
    assert "raise ValueError" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source


def test_currency_policy_has_one_implementation_and_no_access_cycle() -> None:
    currency_source = CURRENCY_OWNER.read_text(encoding="utf-8")
    access_source = ACCESS_OWNER.read_text(encoding="utf-8")

    assert "class PrepaidCurrencyError(DomainError)" in currency_source
    assert "def normalize_prepaid_currency(" in currency_source
    assert "def resolve_prepaid_enforcement_currency(" in currency_source
    assert "from app.services.prepaid_currency import (" in access_source
    assert "def resolve_prepaid_enforcement_currency(" not in access_source
    assert "from app.services.access_resolution" not in (
        THRESHOLD_OWNER.read_text(encoding="utf-8")
    )


def test_service_status_cannot_recreate_threshold_or_paid_coverage_policy() -> None:
    source = SERVICE_STATUS.read_text(encoding="utf-8")

    assert "def _unfunded_prepaid_renewal_requirement(" not in source
    assert "def _paid_prepaid_coverage_end(" not in source
    assert "billing_automation._resolve_price(" not in source
    assert "resolve_prepaid_threshold(" in source
