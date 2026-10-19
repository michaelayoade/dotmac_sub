"""Composable Starter billing policies (ADR 0031): defaults and overrides."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from app.models.catalog import BillingMode
from app.services.billing import starter_policies as policies

_DEFAULTS = {
    "starter_billing_account_ref_template": "subscriber:{subscriber_id}",
    "starter_billing_source_system": "dotmac-sub",
    "starter_billing_admin_source_kind": "admin_manual_invoice_line",
    "starter_billing_admin_contract_line_template": "admin-manual:{line_id}",
    "starter_billing_admin_contract_version": "admin-manual-v1",
    "starter_billing_admin_price_version": "admin-manual-v1",
    "starter_billing_default_charge_component": "other",
    "starter_billing_tax_jurisdiction": "NG",
    "starter_billing_tax_policy_id": "sub-manual-vat",
    "starter_billing_tax_policy_version": "1",
    "starter_billing_due_terms_version": "1",
    "starter_billing_due_derivation_policy": "calendar_days",
    "starter_billing_due_derivation_version": "1",
    "starter_billing_invoice_series": "invoice",
    "starter_billing_credit_note_series": "credit-note",
    "starter_billing_document_profile_code": "invoice.standard",
    "starter_billing_document_profile_version": "1",
    "starter_billing_credit_note_profile_code": "credit_note.standard",
    "starter_billing_default_locale": "en-NG",
    "starter_billing_default_timezone": "Africa/Lagos",
    "starter_billing_default_unit_code": "unit",
    "starter_payments_accepted_evidence": "dotmac-payments:confirmed-cash-v1",
    "starter_billing_admin_source_fact_template": "admin-manual-invoice:{invoice_id}",
    "starter_billing_admin_line_source_fact_template": "admin-manual-invoice-line:{line_id}",
    "starter_billing_invoice_series_prefix": "INV",
    "starter_billing_credit_note_series_prefix": "CN",
    "starter_billing_default_country_code": "NGA",
    "starter_billing_series_start_value": 1,
}


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    values = dict(_DEFAULTS)
    monkeypatch.setattr(
        policies, "resolve_value", lambda _db, _domain, key: values[key]
    )
    return values


def test_defaults_resolve_to_the_declared_policy_surface(settings) -> None:
    resolved = policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    assert resolved.source_system == "dotmac-sub"
    assert resolved.admin_source_kind == "admin_manual_invoice_line"
    assert resolved.tax_jurisdiction == "NG"
    assert resolved.invoice_series == "invoice"
    assert resolved.credit_note_series == "credit-note"
    assert resolved.default_locale == "en-NG"
    assert resolved.default_timezone == "Africa/Lagos"


def test_settings_override_every_default(settings) -> None:
    settings["starter_billing_invoice_series"] = "inv-2027"
    settings["starter_billing_tax_jurisdiction"] = "NG-LAGOS"
    resolved = policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    assert resolved.invoice_series == "inv-2027"
    assert resolved.tax_jurisdiction == "NG-LAGOS"


def test_blank_setting_is_refused_not_defaulted(settings) -> None:
    settings["starter_billing_source_system"] = "  "
    with pytest.raises(policies.StarterBillingPolicyError) as exc_info:
        policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    assert exc_info.value.code.endswith("configuration_invalid")


def test_account_ref_is_per_subscriber(settings) -> None:
    resolved = policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    radio = UUID(int=11)
    fiber = UUID(int=12)
    # Linked accounts under one person stay independent ledger accounts.
    assert resolved.account_ref(radio) == f"subscriber:{radio}"
    assert resolved.account_ref(fiber) == f"subscriber:{fiber}"
    assert resolved.account_ref(radio) != resolved.account_ref(fiber)


def test_account_ref_template_is_configurable(settings) -> None:
    settings["starter_billing_account_ref_template"] = "sub:{subscriber_id}:ngn"
    resolved = policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    assert resolved.account_ref(UUID(int=3)) == f"sub:{UUID(int=3)}:ngn"


def test_account_ref_template_without_placeholder_is_refused(settings) -> None:
    settings["starter_billing_account_ref_template"] = "subscriber:fixed"
    resolved = policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    with pytest.raises(policies.StarterBillingPolicyError):
        resolved.account_ref(UUID(int=3))


def test_admin_contract_line_template_renders_line_identity(settings) -> None:
    resolved = policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    assert resolved.admin_contract_line_ref(UUID(int=5)) == (
        f"admin-manual:{UUID(int=5)}"
    )


def test_collection_timing_maps_billing_modes() -> None:
    assert policies.collection_timing_for_billing_mode(BillingMode.prepaid) == (
        "advance"
    )
    assert policies.collection_timing_for_billing_mode(BillingMode.postpaid) == (
        "arrears"
    )
    assert policies.collection_timing_for_billing_mode("PREPAID") == "advance"
    with pytest.raises(policies.StarterBillingPolicyError):
        policies.collection_timing_for_billing_mode("hybrid")
    with pytest.raises(policies.StarterBillingPolicyError):
        policies.collection_timing_for_billing_mode(None)


def test_due_terms_code_and_percent_conversion() -> None:
    assert policies.due_terms_code(30) == "net_30"
    assert policies.due_terms_code(0) == "net_0"
    with pytest.raises(policies.StarterBillingPolicyError):
        policies.due_terms_code(-1)
    assert policies.percent_to_fraction(Decimal("7.50")) == Decimal("0.075")
    assert policies.percent_to_fraction(Decimal("20.00")) == Decimal("0.2")


def test_issuance_policy_additions_resolve(settings) -> None:
    resolved = policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]
    assert resolved.series_start_value == 1
    assert resolved.series_prefix("invoice") == "INV"
    assert resolved.series_prefix("credit-note") == "CN"
    assert resolved.admin_source_fact_id(UUID(int=4)) == (
        f"admin-manual-invoice:{UUID(int=4)}"
    )
    assert resolved.admin_line_source_fact_id(UUID(int=5)) == (
        f"admin-manual-invoice-line:{UUID(int=5)}"
    )
    assert resolved.default_country_code == "NGA"
    settings["starter_billing_series_start_value"] = 0
    with pytest.raises(policies.StarterBillingPolicyError):
        policies.resolve_starter_billing_policies(None)  # type: ignore[arg-type]


def test_accepted_evidence_is_a_settings_driven_set(settings) -> None:
    assert policies.accepted_confirmation_evidence(None) == frozenset(  # type: ignore[arg-type]
        {"dotmac-payments:confirmed-cash-v1"}
    )
    settings["starter_payments_accepted_evidence"] = "a:1, b:2 ,,"
    assert policies.accepted_confirmation_evidence(None) == frozenset(  # type: ignore[arg-type]
        {"a:1", "b:2"}
    )
