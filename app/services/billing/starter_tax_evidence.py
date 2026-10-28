"""Native Starter Tax adapter; Tax determines, Billing freezes, Sub maps.

Pending the unreleased Billing a10 contract. No legacy rate or customer toggle
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
)
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

if TYPE_CHECKING:
    from dotmac_billing import InvoiceTaxEvidenceV2, TaxDeterminationSetEvidenceV2
    from dotmac_tax import TaxDeterminationSetV2

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
    prior_purposes: tuple[Purpose, ...] = ()


@dataclass(frozen=True, slots=True)
class StarterInvoiceTaxPolicy:
    invoice_tax: TaxPurposeBinding
    expected_withholding: TaxPurposeBinding

    def __post_init__(self) -> None:
        if (
            self.invoice_tax.prior_purposes
            or self.expected_withholding.prior_purposes
            not in (
                (),
                ("invoice_tax",),
            )
        ):
            raise ValueError(
                "Reverse or cyclic Tax purpose dependencies are unsupported"
            )

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
            if (
                not isinstance(row, dict)
                or not required <= set(row)
                or set(row) - required - {"prior_purposes"}
            ):
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
            priors = row.get("prior_purposes", [])
            if not isinstance(priors, list) or priors not in ([], ["invoice_tax"]):
                raise ValueError("Only invoice_tax may be a prior purpose")
            if purpose == "invoice_tax" and priors:
                raise ValueError(
                    "Reverse or cyclic Tax purpose dependencies are unsupported"
                )
            bindings.append(
                TaxPurposeBinding(
                    UUID(row["jurisdiction_id"]),
                    row["fact_kind"],
                    "invoice_expectation",
                    side,
                    row["place_ref"],
                    ("invoice_tax",) if priors else (),
                )
            )
        return cls(*bindings)


def tax_policy_value_error(value: object) -> str | None:
    try:
        StarterInvoiceTaxPolicy.parse(value)
    except (ValueError, TypeError):
        return "Declare valid invoice_tax and expected_withholding invoice-expectation Tax bindings"
    return None


def _billing_determination(
    result: TaxDeterminationSetV2,
) -> TaxDeterminationSetEvidenceV2:
    """Explicit lossless mapping of the public Tax result, without recalculation."""
    from dotmac_billing import (
        TaxBaseDependencyPolicyEvidenceV1,
        TaxCommercialSourceEvidenceV1,
        TaxDependencyComponentEvidenceV1,
        TaxDeterminationComponentEvidenceV2,
        TaxDeterminationLineEvidenceV1,
        TaxDeterminationSetEvidenceV2,
        TaxFactSignatureEvidenceV1,
        TaxPriorResultReferenceEvidenceV1,
    )

    return TaxDeterminationSetEvidenceV2(
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
        commercial_source=TaxCommercialSourceEvidenceV1(
            source_ref=result.commercial_source.source_ref,
            source_version=result.commercial_source.source_version,
        ),
        prior_results=tuple(
            TaxPriorResultReferenceEvidenceV1(
                determination_set_id=prior.determination_set_id,
                source_ref=prior.source_ref,
                source_version=prior.source_version,
                source_fingerprint=prior.source_fingerprint,
                result_fingerprint=prior.result_fingerprint,
            )
            for prior in result.prior_results
        ),
        components=tuple(
            TaxDeterminationComponentEvidenceV2(
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
                selected_dependency_policy=(
                    TaxBaseDependencyPolicyEvidenceV1(
                        prior_signature=TaxFactSignatureEvidenceV1(
                            jurisdiction_id=component.selected_dependency_policy.prior_signature.jurisdiction_id,
                            fact_kind=component.selected_dependency_policy.prior_signature.fact_kind,
                            recognition_basis_code=component.selected_dependency_policy.prior_signature.recognition_basis_code,
                            transaction_side=component.selected_dependency_policy.prior_signature.transaction_side,
                        ),
                        selection=component.selected_dependency_policy.selection,
                        tax_code_ids=component.selected_dependency_policy.tax_code_ids,
                    )
                    if component.selected_dependency_policy is not None
                    else None
                ),
                dependency_components=tuple(
                    TaxDependencyComponentEvidenceV1(
                        determination_set_id=dependency.determination_set_id,
                        determination_id=dependency.determination_id,
                        tax_code_id=dependency.tax_code_id,
                        rule_id=dependency.rule_id,
                        rule_version=dependency.rule_version,
                        component_sequence=dependency.component_sequence,
                        tax_amount=dependency.tax_amount,
                    )
                    for dependency in component.dependency_components
                ),
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
    )


def billing_evidence(
    result: TaxDeterminationSetV2,
    purpose: Purpose,
    *,
    prior_results: tuple[TaxDeterminationSetV2, ...] = (),
) -> InvoiceTaxEvidenceV2:
    """Copy public Tax results and opaque owner seals, never rebuild their preimage."""
    from dotmac_billing import InvoiceTaxEvidenceV2

    return InvoiceTaxEvidenceV2(
        purpose=purpose,
        determination=_billing_determination(result),
        prior_determinations=tuple(
            _billing_determination(prior) for prior in prior_results
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
) -> tuple[InvoiceTaxEvidenceV2, ...]:
    try:
        from dotmac_tax import (
            TaxCommercialSourceV1,
            TaxFactV2,
            TaxPriorResultReferenceV1,
            determine_tax_set_v2,
        )
    except ImportError as exc:
        raise DomainError(
            code="financial.starter_issuance.tax_contract_pending",
            message="Native dependency Tax issuance requires the Tax V2 contract",
        ) from exc
    evidence: list[InvoiceTaxEvidenceV2] = []
    results: dict[Purpose, TaxDeterminationSetV2] = {}
    commercial_source = TaxCommercialSourceV1(source_ref, source_version)
    for purpose in _PURPOSES:
        binding = (
            policy.invoice_tax
            if purpose == "invoice_tax"
            else policy.expected_withholding
        )
        try:
            priors = tuple(results[prior] for prior in binding.prior_purposes)
            result = determine_tax_set_v2(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                fact=TaxFactV2(
                    commercial_source=commercial_source,
                    prior_results=tuple(
                        TaxPriorResultReferenceV1(
                            determination_set_id=prior.determination_set_id,
                            source_ref=prior.source_ref,
                            source_version=prior.source_version,
                            source_fingerprint=prior.source_fingerprint,
                            result_fingerprint=prior.result_fingerprint,
                        )
                        for prior in priors
                    ),
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
        results[purpose] = result
        evidence.append(billing_evidence(result, purpose, prior_results=priors))
    return tuple(evidence)
