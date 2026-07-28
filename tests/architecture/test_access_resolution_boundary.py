"""Keep billing and RADIUS access decisions behind one contracted owner."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import all_services, dependencies_for

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "access_resolution.py"
RETIRED_IMPLEMENTATION = PROJECT_ROOT / "app" / "services" / "customer_service_state.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_access_resolution_has_one_complete_registry_identity() -> None:
    services = all_services()
    owners = [
        service
        for service in services
        if service.module == "app.services.access_resolution"
    ]

    assert [service.name for service in owners] == ["financial.access_resolution"]
    service = owners[0]
    assert service.is_contracted
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "read_only"
    assert service.contract.migration.state.value == "complete"
    assert {concern.name for concern in service.contract.concerns} == set(service.owns)
    assert "financial.access_resolution" in dependencies_for("access.radius_state")


def test_legacy_customer_service_module_no_longer_implements_access_policy() -> None:
    source = _source(RETIRED_IMPLEMENTATION)

    retired_symbols = (
        "CustomerBillingAccessState",
        "resolve_customer_billing_access_state",
        "active_customer_subscription_filters",
        "postpaid_invoice_eligible_filters",
        "prepaid_enforcement_eligible_filters",
    )
    for symbol in retired_symbols:
        assert symbol not in source


def test_owner_exposes_typed_outcomes_and_delegates_currency_policy() -> None:
    source = _source(OWNER)

    assert "class SubscriptionAccessInput(Protocol)" in source
    assert "class SubscriberAccessInput(Protocol)" in source
    assert "class CustomerBillingAccessState:" in source
    assert "class PrepaidFundingDecision:" in source
    assert "from app.services.prepaid_currency import (" in source
    assert "def resolve_prepaid_enforcement_currency" not in source
    assert "class AccessResolutionError" not in source
    assert "HTTPException" not in source
    assert "raise ValueError" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
