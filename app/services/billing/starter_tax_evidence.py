"""Native Starter Tax adapter; Tax determines, Billing freezes, Sub maps.

Pending the unreleased Billing a9 contract. No legacy rate or customer toggle
is an input. Both purposes require published policies, including explicit zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from dotmac_kernel.money import Money
from dotmac_tax import (
    TaxConflict,
    TaxFact,
    TaxNotFound,
    TaxRuleViolation,
    determine_tax_set,
)
from dotmac_tax.contracts import TaxDeterminationSetV1
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

if TYPE_CHECKING:
    from dotmac_billing import InvoiceTaxEvidenceV1

Purpose = Literal["invoice_tax", "expected_withholding"]
_PURPOSES: tuple[Purpose, ...] = ("invoice_tax", "expected_withholding")
SETTING_KEY = "starter_invoice_tax_policy"


@dataclass(frozen=True, slots=True)
class TaxPurposeBinding:
    jurisdiction_id: UUID
    fact_kind: str
    recognition_basis_code: Literal["invoice_expectation"]
    transaction_side: Literal["output", "withholding"]
    place_ref: str


@dataclass(frozen=True, slots=True)
class StarterInvoiceTaxPolicy:
    invoice_tax: TaxPurposeBinding
    expected_withholding: TaxPurposeBinding

    @classmethod
    def parse(cls, value: object) -> StarterInvoiceTaxPolicy:
        if not isinstance(value, dict) or set(value) != {
            "invoice_tax",
            "expected_withholding",
        }:
            raise ValueError(
                "Both invoice_tax and expected_withholding bindings are required"
            )
        bindings: list[TaxPurposeBinding] = []
        for purpose in _PURPOSES:
            row = value[purpose]
            required = {
                "jurisdiction_id",
                "fact_kind",
                "recognition_basis_code",
                "transaction_side",
                "place_ref",
            }
            if not isinstance(row, dict) or set(row) != required:
                raise ValueError(
                    "Tax binding must declare jurisdiction, fact kind, recognition, side and place reference"
                )
            if any(
                not isinstance(row[key], str) or not row[key].strip()
                for key in required
            ):
                raise ValueError("Tax binding values must be nonempty strings")
            side: Literal["output", "withholding"] = (
                "output" if purpose == "invoice_tax" else "withholding"
            )
            if (
                row["recognition_basis_code"] != "invoice_expectation"
                or row["transaction_side"] != side
            ):
                raise ValueError(
                    "Invoice Tax binding requires invoice_expectation recognition and the purpose-specific transaction side"
                )
            bindings.append(
                TaxPurposeBinding(
                    UUID(row["jurisdiction_id"]),
                    row["fact_kind"],
                    "invoice_expectation",
                    side,
                    row["place_ref"],
                )
            )
        return cls(*bindings)


def tax_policy_value_error(value: object) -> str | None:
    try:
        StarterInvoiceTaxPolicy.parse(value)
    except (ValueError, TypeError):
        return "Declare valid invoice_tax and expected_withholding invoice-expectation Tax bindings"
    return None


def billing_evidence(
    result: TaxDeterminationSetV1, purpose: Purpose
) -> InvoiceTaxEvidenceV1:
    """Explicit lossless mapping of the public Tax result, without recalculation."""
    from dotmac_billing import (
        InvoiceTaxEvidenceV1,
        TaxDeterminationComponentEvidenceV1,
        TaxDeterminationLineEvidenceV1,
        TaxDeterminationSetEvidenceV1,
    )

    return InvoiceTaxEvidenceV1(
        purpose=purpose,
        determination=TaxDeterminationSetEvidenceV1(
            tenant_id=result.tenant_id,
            determination_set_id=result.determination_set_id,
            jurisdiction_id=result.jurisdiction_id,
            occurred_on=result.occurred_on,
            fact_kind=result.fact_kind,
            recognition_basis_code=result.recognition_basis_code,
            transaction_side=result.transaction_side,
            source_amount=result.source_amount,
            net_amount=result.net_amount,
            tax_amount=result.tax_amount,
            gross_amount=result.gross_amount,
            source_ref=result.source_ref,
            source_version=result.source_version,
            source_fingerprint=result.source_fingerprint,
            result_fingerprint=result.result_fingerprint,
            evidence_ref=result.evidence_ref,
            counterparty_ref=result.counterparty_ref,
            supply_ref=result.supply_ref,
            place_ref=result.place_ref,
            determined_at=result.determined_at,
            components=tuple(
                TaxDeterminationComponentEvidenceV1(
                    determination_id=component.determination_id,
                    determination_set_id=component.determination_set_id,
                    component_sequence=component.component_sequence,
                    tax_code_id=component.tax_code_id,
                    rule_id=component.rule_id,
                    rule_version=component.rule_version,
                    treatment_code=component.treatment_code,
                    calculation_base_code=component.calculation_base_code,
                    inclusive=component.inclusive,
                    party_category=component.party_category,
                    supply_category=component.supply_category,
                    place_code=component.place_code,
                    party_classification_id=component.party_classification_id,
                    supply_classification_id=component.supply_classification_id,
                    place_classification_id=component.place_classification_id,
                    base_amount=component.base_amount,
                    tax_amount=component.tax_amount,
                    recoverable_amount=component.recoverable_amount,
                    non_recoverable_amount=component.non_recoverable_amount,
                    lines=tuple(
                        TaxDeterminationLineEvidenceV1(
                            sequence=line.sequence,
                            taxable_amount=line.taxable_amount,
                            rate=line.rate,
                            tax_amount=line.tax_amount,
                        )
                        for line in component.lines
                    ),
                )
                for component in result.components
            ),
        ),
    )


def determine_invoice_line_tax(
    db: Session,
    *,
    policy: StarterInvoiceTaxPolicy,
    amount: Money,
    source_ref: str,
    source_version: str,
    counterparty_ref: str,
    supply_ref: str,
    issued_at: datetime,
    occurred_on: date,
) -> tuple[InvoiceTaxEvidenceV1, ...]:
    evidence: list[InvoiceTaxEvidenceV1] = []
    for purpose in _PURPOSES:
        binding = (
            policy.invoice_tax
            if purpose == "invoice_tax"
            else policy.expected_withholding
        )
        try:
            result = determine_tax_set(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                fact=TaxFact(
                    jurisdiction_id=binding.jurisdiction_id,
                    occurred_on=occurred_on,
                    fact_kind=binding.fact_kind,
                    recognition_basis_code=binding.recognition_basis_code,
                    transaction_side=binding.transaction_side,
                    base_amount=amount,
                    source_ref=f"{source_ref}:tax:{purpose}",
                    source_version=source_version,
                    evidence_ref=source_ref,
                    counterparty_ref=counterparty_ref,
                    supply_ref=supply_ref,
                    place_ref=binding.place_ref,
                ),
                determined_at=issued_at,
            )
        except (TaxConflict, TaxNotFound, TaxRuleViolation) as exc:
            raise DomainError(
                code="financial.starter_issuance.tax_refused",
                message="Published Tax policy refused native invoice determination",
                details={"purpose": purpose},
            ) from exc
        if purpose == "invoice_tax" and result.net_amount != amount:
            raise DomainError(
                code="financial.starter_issuance.tax_policy_unsupported",
                message="Native invoice source must remain the commercial pre-tax amount",
                retryable=False,
            )
        if purpose == "expected_withholding" and any(
            component.calculation_base_code != "source_amount"
            for component in result.components
        ):
            raise DomainError(
                code="financial.starter_issuance.tax_policy_unsupported",
                message="Cross-purpose withholding compounding requires a Tax purpose-routing contract",
                retryable=False,
            )
        evidence.append(billing_evidence(result, purpose))
    return tuple(evidence)
