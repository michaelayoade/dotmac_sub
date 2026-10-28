"""Native Tax adapter contracts; requires the pending Billing a10 package."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest
from dotmac_kernel.money import Currency, Money
from dotmac_tax import (
    TaxBaseDependencyPolicyV1,
    TaxCommercialSourceV1,
    TaxDependencyComponentV1,
    TaxDeterminationComponentV2,
    TaxDeterminationLineV1,
    TaxDeterminationSetV2,
    TaxFactSignatureV1,
    TaxPriorResultReferenceV1,
)

from app.services.billing.starter_tax_evidence import (
    StarterInvoiceTaxPolicy,
    billing_evidence,
)


def _determination() -> TaxDeterminationSetV2:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    currency = Currency("NGN", 2)

    def money(value: str) -> Money:
        return Money.of(value, currency)

    set_id = uuid4()
    component = TaxDeterminationComponentV2(
        determination_id=uuid4(),
        determination_set_id=set_id,
        component_sequence=1,
        tax_code_id=uuid4(),
        rule_id=uuid4(),
        rule_version=7,
        treatment_code="standard_rated",
        calculation_base_code="source_amount",
        inclusive=False,
        party_category="business",
        supply_category="service",
        place_code="fixture",
        party_classification_id=uuid4(),
        supply_classification_id=uuid4(),
        place_classification_id=uuid4(),
        base_amount=money("100.00"),
        tax_amount=money("7.50"),
        recoverable_amount=money("0.00"),
        non_recoverable_amount=money("7.50"),
        lines=(
            TaxDeterminationLineV1(1, money("100.00"), Decimal("0.075"), money("7.50")),
        ),
    )
    return TaxDeterminationSetV2(
        tenant_id=uuid4(),
        determination_set_id=set_id,
        jurisdiction_id=uuid4(),
        occurred_on=now.date(),
        fact_kind="invoice",
        recognition_basis_code="invoice_expectation",
        transaction_side="output",
        source_amount=money("100.00"),
        net_amount=money("100.00"),
        tax_amount=money("7.50"),
        gross_amount=money("107.50"),
        source_ref="source:immutable",
        source_version="v7",
        source_fingerprint="a" * 64,
        result_fingerprint="rv2:" + "a" * 64,
        evidence_ref="evidence:7",
        counterparty_ref="party:7",
        supply_ref="service:7",
        place_ref="place:7",
        determined_at=now,
        components=(component,),
        commercial_source=TaxCommercialSourceV1("original:line:7", "v7"),
        prior_results=(),
    )


def test_maps_every_set_component_and_line_field_without_loss() -> None:
    original = _determination()
    mapped = billing_evidence(original, "invoice_tax")
    assert mapped.purpose == "invoice_tax"
    assert asdict(mapped.determination) == asdict(original)


@pytest.mark.parametrize(
    "value",
    (None, {}, {"invoice_tax": {}}, {"invoice_tax": {}, "expected_withholding": {}}),
)
def test_policy_requires_explicit_both_purposes(value: object) -> None:
    with pytest.raises(ValueError):
        StarterInvoiceTaxPolicy.parse(value)


@pytest.mark.parametrize("selection", ("all_prior", "tax_code_ids"))
def test_maps_complete_prior_results_and_dependency_selection(
    selection: Literal["all_prior", "tax_code_ids"],
) -> None:
    prior = _determination()
    original = prior.components[0]
    currency = prior.source_amount.currency
    policy = TaxBaseDependencyPolicyV1(
        prior_signature=TaxFactSignatureV1(
            prior.jurisdiction_id,
            prior.fact_kind,
            prior.recognition_basis_code,
            prior.transaction_side,
        ),
        selection=selection,
        tax_code_ids=(original.tax_code_id,) if selection == "tax_code_ids" else (),
    )
    set_id = uuid4()
    component = replace(
        original,
        determination_id=uuid4(),
        determination_set_id=set_id,
        calculation_base_code="source_plus_dependency_tax",
        base_amount=Money.of("107.50", currency),
        tax_amount=Money.of("5.38", currency),
        non_recoverable_amount=Money.of("5.38", currency),
        lines=(
            TaxDeterminationLineV1(
                1,
                Money.of("107.50", currency),
                Decimal("0.05"),
                Money.of("5.38", currency),
            ),
        ),
        selected_dependency_policy=policy,
        dependency_components=(
            TaxDependencyComponentV1(
                prior.determination_set_id,
                original.determination_id,
                original.tax_code_id,
                original.rule_id,
                original.rule_version,
                original.component_sequence,
                original.tax_amount,
            ),
        ),
    )
    result = replace(
        prior,
        determination_set_id=set_id,
        transaction_side="withholding",
        source_ref="source:immutable:wht",
        fact_kind="withholding",
        tax_amount=Money.of("5.38", currency),
        gross_amount=Money.of("105.38", currency),
        components=(component,),
        prior_results=(
            TaxPriorResultReferenceV1(
                prior.determination_set_id,
                prior.source_ref,
                prior.source_version,
                prior.source_fingerprint,
                prior.result_fingerprint,
            ),
        ),
    )
    mapped = billing_evidence(result, "expected_withholding", prior_results=(prior,))
    assert asdict(mapped.determination) == asdict(result)
    assert tuple(asdict(item) for item in mapped.prior_determinations) == (
        asdict(prior),
    )
    with pytest.raises(ValueError, match="prior result"):
        billing_evidence(result, "expected_withholding")


def _policy_value(priors: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        purpose: {
            "jurisdiction_id": str(uuid4()),
            "fact_kind": purpose,
            "recognition_basis_code": "invoice_expectation",
            "transaction_side": side,
            "place_ref": "place:7",
            "prior_purposes": priors.get(purpose, []),
        }
        for purpose, side in (
            ("invoice_tax", "output"),
            ("expected_withholding", "withholding"),
        )
    }


def test_policy_routes_prior_purpose_without_selecting_numeric_components() -> None:
    policy = StarterInvoiceTaxPolicy.parse(
        _policy_value({"expected_withholding": ["invoice_tax"]})
    )
    assert policy.invoice_tax.prior_purposes == ()
    assert policy.expected_withholding.prior_purposes == ("invoice_tax",)


@pytest.mark.parametrize(
    "priors",
    (
        {"invoice_tax": ["expected_withholding"]},
        {"invoice_tax": ["invoice_tax"]},
        {"expected_withholding": ["expected_withholding"]},
        {"expected_withholding": ["invoice_tax", "invoice_tax"]},
        {"expected_withholding": "invoice_tax"},
    ),
)
def test_policy_refuses_cycles_reverse_unknown_and_duplicate_dependencies(
    priors: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        StarterInvoiceTaxPolicy.parse(_policy_value(priors))
