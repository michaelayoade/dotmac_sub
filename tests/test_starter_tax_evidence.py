"""Native Tax adapter contracts; requires the pending Billing a9 package."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_kernel.money import Currency, Money
from dotmac_tax import (
    TaxDeterminationComponentV1,
    TaxDeterminationLineV1,
    TaxDeterminationSetV1,
)

from app.services.billing.starter_tax_evidence import (
    StarterInvoiceTaxPolicy,
    billing_evidence,
)


def _determination() -> TaxDeterminationSetV1:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    currency = Currency("NGN", 2)

    def money(value: str) -> Money:
        return Money.of(value, currency)

    set_id = uuid4()
    component = TaxDeterminationComponentV1(
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
    return TaxDeterminationSetV1(
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
        result_fingerprint="rv1:" + "a" * 64,
        evidence_ref="evidence:7",
        counterparty_ref="party:7",
        supply_ref="service:7",
        place_ref="place:7",
        determined_at=now,
        components=(component,),
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
