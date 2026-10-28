"""Typed staff authoring inputs; Tax remains the policy validation owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal
from uuid import UUID

from dotmac_kernel.money import Currency, Money, MoneyError
from dotmac_tax import (
    TaxBaseDependencyPolicyV1,
    TaxCommercialSourceV1,
    TaxFact,
    TaxFactSignatureV1,
    TaxPolicyPreviewV1,
    TaxPriorResultReferenceV1,
    TaxRuleBandInput,
    TaxRuleInputV2,
)
from pydantic import BaseModel, ConfigDict, Field


class Operation(StrEnum):
    authority = "authority"
    jurisdiction = "jurisdiction"
    code = "code"
    initial_rule = "initial-rule"
    supersede = "supersede"
    preview = "preview"


class FormInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, allow_inf_nan=False
    )


class Provenance(FormInput):
    reason: str = Field(min_length=1, max_length=1000)
    evidence_ref: str = Field(min_length=1, max_length=255)
    source_ref: str = Field(min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=100)


class AuthorityDraft(Provenance):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    authority_level_code: str | None = None


class JurisdictionDraft(Provenance):
    authority_id: UUID
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    country_code: str = Field(min_length=2, max_length=2)
    currency_code: str = Field(min_length=3, max_length=3)
    minor_units: int = Field(ge=0, le=6)
    subdivision_code: str | None = None


class CodeDraft(Provenance):
    jurisdiction_code: str = Field(min_length=1, max_length=100)
    jurisdiction_id: UUID
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    tax_kind_code: str = Field(min_length=1, max_length=100)
    description: str | None = None


class BandDraft(FormInput):
    sequence: int
    lower_bound: Decimal
    upper_bound: Decimal | None = None
    rate: Decimal


class DependencySelection(StrEnum):
    none = "none"
    all_prior = "all_prior"
    tax_code_ids = "tax_code_ids"


class PolicyIdentity(FormInput):
    tax_code_id: UUID
    jurisdiction_id: UUID
    jurisdiction_code: str = Field(min_length=1, max_length=100)
    tax_code: str = Field(min_length=1, max_length=100)


class RuleDraft(Provenance):
    jurisdiction_id: UUID
    jurisdiction_code: str = Field(min_length=1, max_length=100)
    tax_code: str = Field(min_length=1, max_length=100)
    tax_code_id: UUID
    version: int = Field(ge=1)
    effective_from: date
    effective_to: date | None = None
    priority: int
    fact_kind: str = Field(min_length=1, max_length=100)
    recognition_basis_code: str = Field(min_length=1, max_length=100)
    transaction_side: str = Field(min_length=1, max_length=100)
    calculation_method: str = Field(min_length=1, max_length=100)
    rate: Decimal | None = None
    fixed_amount: Decimal | None = None
    currency_code: str = Field(min_length=3, max_length=3)
    minor_units: int = Field(ge=0, le=6)
    inclusive: bool
    recoverable_rate: Decimal
    party_category: str | None = None
    supply_category: str | None = None
    place_code: str | None = None
    bands: tuple[BandDraft, ...] = Field(default=(), max_length=100)
    treatment_code: str = Field(min_length=1, max_length=100)
    calculation_sequence: int
    calculation_base_code: str = Field(min_length=1, max_length=100)

    dependency_selection: DependencySelection = DependencySelection.none
    dependency_prior_jurisdiction_id: UUID | None = None
    dependency_prior_fact_kind: str | None = Field(default=None, max_length=100)
    dependency_prior_recognition_basis_code: str | None = Field(
        default=None, max_length=100
    )
    dependency_prior_transaction_side: str | None = Field(default=None, max_length=100)
    dependency_tax_code_ids: tuple[UUID, ...] = Field(default=(), max_length=100)

    def dependency_input(self) -> TaxBaseDependencyPolicyV1 | None:
        if self.dependency_selection is DependencySelection.none:
            if (
                self.dependency_prior_jurisdiction_id
                or self.dependency_prior_fact_kind
                or self.dependency_prior_recognition_basis_code
                or self.dependency_prior_transaction_side
                or self.dependency_tax_code_ids
            ):
                raise ValueError(
                    "Choose a dependency selection or clear the dependency inputs."
                )
            return None
        if not (
            self.dependency_prior_jurisdiction_id
            and self.dependency_prior_fact_kind
            and self.dependency_prior_recognition_basis_code
            and self.dependency_prior_transaction_side
        ):
            raise ValueError(
                "A dependent policy requires one complete prior jurisdiction/fact kind/recognition/side signature."
            )
        return TaxBaseDependencyPolicyV1(
            prior_signature=TaxFactSignatureV1(
                jurisdiction_id=self.dependency_prior_jurisdiction_id,
                fact_kind=self.dependency_prior_fact_kind,
                recognition_basis_code=self.dependency_prior_recognition_basis_code,
                transaction_side=self.dependency_prior_transaction_side,
            ),
            selection="all_prior"
            if self.dependency_selection is DependencySelection.all_prior
            else "tax_code_ids",
            tax_code_ids=self.dependency_tax_code_ids,
        )

    def tax_input(self) -> TaxRuleInputV2:
        return TaxRuleInputV2(
            tax_code_id=self.tax_code_id,
            version=self.version,
            effective_from=self.effective_from,
            effective_to=self.effective_to,
            priority=self.priority,
            fact_kind=self.fact_kind,
            recognition_basis_code=self.recognition_basis_code,
            transaction_side=self.transaction_side,
            calculation_method=self.calculation_method,
            rate=self.rate,
            fixed_amount=exact_money(
                self.fixed_amount, self.currency_code, self.minor_units
            )
            if self.fixed_amount is not None
            else None,
            inclusive=self.inclusive,
            recoverable_rate=self.recoverable_rate,
            party_category=self.party_category,
            supply_category=self.supply_category,
            place_code=self.place_code,
            bands=tuple(
                TaxRuleBandInput(
                    sequence=b.sequence,
                    lower_bound=b.lower_bound,
                    upper_bound=b.upper_bound,
                    rate=b.rate,
                )
                for b in self.bands
            ),
            treatment_code=self.treatment_code,
            calculation_sequence=self.calculation_sequence,
            calculation_base_code=self.calculation_base_code,
            dependency_policy=self.dependency_input(),
        )


class SupersessionDraft(RuleDraft):
    predecessor_rule_id: UUID
    predecessor_version: int = Field(ge=1)


class PreviewDraft(FormInput):
    preview_mode: Literal["current", "dependency"] = "current"
    prior_determination_set_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    jurisdiction_id: UUID
    occurred_on: date
    fact_kind: str = Field(min_length=1, max_length=100)
    recognition_basis_code: str = Field(min_length=1, max_length=100)
    transaction_side: str = Field(min_length=1, max_length=100)
    base_amount: Decimal
    currency_code: str = Field(min_length=3, max_length=3)
    minor_units: int = Field(ge=0, le=6)
    source_ref: str = Field(min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=100)
    evidence_ref: str = Field(min_length=1, max_length=255)
    party_category: str | None = None
    supply_category: str | None = None
    place_code: str | None = None
    counterparty_ref: str | None = None
    supply_ref: str | None = None
    place_ref: str | None = None

    def tax_fact(self) -> TaxFact:
        return TaxFact(
            jurisdiction_id=self.jurisdiction_id,
            occurred_on=self.occurred_on,
            fact_kind=self.fact_kind,
            recognition_basis_code=self.recognition_basis_code,
            transaction_side=self.transaction_side,
            base_amount=exact_money(
                self.base_amount, self.currency_code, self.minor_units
            ),
            source_ref=self.source_ref,
            source_version=self.source_version,
            evidence_ref=self.evidence_ref,
            party_category=self.party_category,
            supply_category=self.supply_category,
            place_code=self.place_code,
            counterparty_ref=self.counterparty_ref,
            supply_ref=self.supply_ref,
            place_ref=self.place_ref,
        )


def exact_money(amount: Decimal, currency_code: str, minor_units: int) -> Money:
    if not amount.is_finite():
        raise ValueError("A finite monetary amount is required")
    try:
        result = Money.of(amount, Currency(currency_code, minor_units))
    except (MoneyError, InvalidOperation) as exc:
        raise ValueError("Invalid monetary amount or currency") from exc
    if result.amount != amount:
        raise ValueError("Monetary input exceeds the currency's minor-unit precision")
    return result


AuthoringDraft = (
    AuthorityDraft | JurisdictionDraft | CodeDraft | RuleDraft | SupersessionDraft
)


@dataclass(frozen=True, slots=True)
class AuthoringOutcome:
    entity_id: UUID
    operation: Operation
    jurisdiction_id: UUID | None = None
    tax_code_id: UUID | None = None
    jurisdiction_code: str | None = None
    tax_code: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyPreviewOutcome:
    result: TaxPolicyPreviewV1
    commercial_source: TaxCommercialSourceV1 | None = None
    prior_references: tuple[TaxPriorResultReferenceV1, ...] = ()
