"""Recurring and prepaid billing consume one compatibility tax resolver."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_billing_tax_resolution_has_one_typed_read_only_owner() -> None:
    owner = service_relationship("financial.billing_tax_resolution")

    assert owner.module == "app.services.billing_tax_resolution"
    assert owner.contract is not None
    assert owner.contract.transaction.mode.value == "read_only"
    assert "financial.customer_tax_policies" in owner.depends_on
    assert "financial.tax_configuration" in owner.depends_on


def test_recurring_and_prepaid_paths_delegate_tax_selection() -> None:
    recurring = _source("app/services/billing_automation.py")
    prepaid = _source("app/services/prepaid_service_renewals.py")

    assert "resolve_subscription_tax(" in recurring
    assert "resolve_subscription_taxes(" in prepaid
    assert "CustomerTaxPolicy" not in recurring
    assert "CustomerTaxPolicy" not in prepaid
    assert "offer.vat_percent" not in prepaid
    assert "address_tax_ids" not in prepaid


def test_catalog_compatibility_tax_copy_does_not_claim_prices_include_vat() -> None:
    form = _source("templates/admin/catalog/offer_form.html")
    detail = _source("templates/admin/catalog/offer_detail.html")
    calculator = _source("templates/admin/catalog/calculator.html")

    assert "Prices include VAT" not in form
    assert "VAT Included" not in calculator
    assert "Taxable under the billing VAT policy" in form
    assert "does not mean the displayed price includes VAT" in form
    assert "Legacy VAT percentage" in form
    assert "Legacy VAT taxable" in detail
    assert "Taxable under VAT policy" in calculator
