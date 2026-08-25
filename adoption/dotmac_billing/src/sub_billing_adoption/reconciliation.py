"""Exact three-way shadow reconciliation with explicit drift disposition."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sub_billing_adoption.errors import AdoptionErrorCode, BillingAdoptionError


class FinancialSurface(StrEnum):
    DOCUMENT = "document"
    SETTLEMENT = "settlement"
    ALLOCATION = "allocation"
    POSITION = "position"
    ACCOUNTING_FACT = "accounting_fact"
    TAX_FX = "tax_fx"


class ObservationSource(StrEnum):
    LEGACY = "legacy"
    BILLING_TARGET = "billing_target"
    INDEPENDENT_CONTROL = "independent_control"


class DriftClassification(StrEnum):
    MATCH = "match"
    SOURCE_DEFECT = "source_defect"
    INTENTIONAL_CORRECTION = "known_intentional_correction"
    MISSING_EVIDENCE = "missing_evidence"
    CONTRACT_DEFECT = "contract_defect"
    SHADOW_BUG = "shadow_bug"
    UNCLASSIFIED = "unclassified"


class CustomerImpact(StrEnum):
    NONE = "none"
    CUSTOMER_DEBIT = "customer_debit"
    OVER_CREDIT = "over_credit"
    TAX = "tax"
    ACCESS = "access"


@dataclass(frozen=True, slots=True, order=True)
class ComparisonKeyV1:
    tenant_id: UUID
    account_id: UUID
    currency: str
    surface: FinancialSurface
    source_identity: str


@dataclass(frozen=True, slots=True)
class FinancialObservationV1:
    key: ComparisonKeyV1
    source: ObservationSource
    digest_sha256: str

    def __post_init__(self) -> None:
        if len(self.digest_sha256) != 64:
            raise BillingAdoptionError(
                AdoptionErrorCode.INVALID_SOURCE_FACT,
                "reconciliation observations require a SHA-256 digest",
                context={"source_identity": self.key.source_identity},
            )


@dataclass(frozen=True, slots=True)
class DriftAcceptanceV1:
    key: ComparisonKeyV1
    classification: DriftClassification
    impact: CustomerImpact
    review_reference: str | None
    rationale: str

    def __post_init__(self) -> None:
        if self.classification in {
            DriftClassification.MATCH,
            DriftClassification.UNCLASSIFIED,
        }:
            raise BillingAdoptionError(
                AdoptionErrorCode.INVALID_DRIFT_ACCEPTANCE,
                "a reviewed mismatch needs a concrete non-match classification",
                context={"source_identity": self.key.source_identity},
            )
        if not self.rationale.strip():
            raise BillingAdoptionError(
                AdoptionErrorCode.INVALID_DRIFT_ACCEPTANCE,
                "a drift acceptance requires a rationale",
                context={"source_identity": self.key.source_identity},
            )
        if self.impact is not CustomerImpact.NONE and not (
            self.review_reference and self.review_reference.strip()
        ):
            raise BillingAdoptionError(
                AdoptionErrorCode.INVALID_DRIFT_ACCEPTANCE,
                "customer-impacting drift requires a Finance/product review reference",
                context={"source_identity": self.key.source_identity},
            )


@dataclass(frozen=True, slots=True)
class ComparisonResultV1:
    key: ComparisonKeyV1
    classification: DriftClassification
    impact: CustomerImpact
    missing_sources: tuple[ObservationSource, ...]
    legacy_digest: str | None
    target_digest: str | None
    control_digest: str | None
    review_reference: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationReportV1:
    run_id: UUID
    observed_at: datetime
    results: tuple[ComparisonResultV1, ...]
    unclassified_count: int
    report_fingerprint: str

    @property
    def complete(self) -> bool:
        return self.unclassified_count == 0 and bool(self.results)


_ALL_SOURCES = frozenset(ObservationSource)


def _report_fingerprint(results: tuple[ComparisonResultV1, ...]) -> str:
    rows = (
        "\x1e".join(
            (
                str(result.key.tenant_id),
                str(result.key.account_id),
                result.key.currency,
                result.key.surface.value,
                result.key.source_identity,
                result.classification.value,
                result.impact.value,
                result.legacy_digest or "",
                result.target_digest or "",
                result.control_digest or "",
                result.review_reference or "",
                ",".join(source.value for source in result.missing_sources),
            )
        )
        for result in results
    )
    return hashlib.sha256("\x1f".join(rows).encode()).hexdigest()


def reconcile_shadow(
    *,
    run_id: UUID,
    observed_at: datetime,
    observations: tuple[FinancialObservationV1, ...],
    acceptances: tuple[DriftAcceptanceV1, ...] = (),
) -> ReconciliationReportV1:
    """Compare legacy, Billing, and independent-control digests exactly.

    No numeric tolerance exists here. The digest input must already preserve
    exact Decimal values and typed provenance.
    """

    grouped: dict[ComparisonKeyV1, dict[ObservationSource, str]] = {}
    for observation in observations:
        sources = grouped.setdefault(observation.key, {})
        if observation.source in sources:
            raise BillingAdoptionError(
                AdoptionErrorCode.DUPLICATE_RECONCILIATION_OBSERVATION,
                "one comparison key may have one observation from each source",
                context={
                    "source_identity": observation.key.source_identity,
                    "source": observation.source.value,
                },
            )
        sources[observation.source] = observation.digest_sha256

    accepted_by_key = {acceptance.key: acceptance for acceptance in acceptances}
    if len(accepted_by_key) != len(acceptances):
        raise BillingAdoptionError(
            AdoptionErrorCode.INVALID_DRIFT_ACCEPTANCE,
            "a comparison key may have only one reviewed drift acceptance",
        )

    results: list[ComparisonResultV1] = []
    mismatched: set[ComparisonKeyV1] = set()
    for key in sorted(grouped):
        sources = grouped[key]
        missing = tuple(
            sorted(_ALL_SOURCES - sources.keys(), key=lambda item: item.value)
        )
        digests = frozenset(sources.values())
        if not missing and len(digests) == 1:
            classification = DriftClassification.MATCH
            impact = CustomerImpact.NONE
            review_reference = None
        else:
            mismatched.add(key)
            acceptance = accepted_by_key.get(key)
            if acceptance is None:
                classification = DriftClassification.UNCLASSIFIED
                impact = CustomerImpact.NONE
                review_reference = None
            else:
                classification = acceptance.classification
                impact = acceptance.impact
                review_reference = acceptance.review_reference
        results.append(
            ComparisonResultV1(
                key=key,
                classification=classification,
                impact=impact,
                missing_sources=missing,
                legacy_digest=sources.get(ObservationSource.LEGACY),
                target_digest=sources.get(ObservationSource.BILLING_TARGET),
                control_digest=sources.get(ObservationSource.INDEPENDENT_CONTROL),
                review_reference=review_reference,
            )
        )

    extra_acceptances = set(accepted_by_key) - mismatched
    if extra_acceptances:
        rendered = ",".join(key.source_identity for key in sorted(extra_acceptances))
        raise BillingAdoptionError(
            AdoptionErrorCode.INVALID_DRIFT_ACCEPTANCE,
            "reviewed drift acceptance names a comparison that currently matches",
            context={"source_identities": rendered},
        )

    frozen = tuple(results)
    unclassified = sum(
        result.classification is DriftClassification.UNCLASSIFIED for result in frozen
    )
    return ReconciliationReportV1(
        run_id=run_id,
        observed_at=observed_at,
        results=frozen,
        unclassified_count=unclassified,
        report_fingerprint=_report_fingerprint(frozen),
    )


__all__ = [
    "ComparisonKeyV1",
    "ComparisonResultV1",
    "CustomerImpact",
    "DriftAcceptanceV1",
    "DriftClassification",
    "FinancialObservationV1",
    "FinancialSurface",
    "ObservationSource",
    "ReconciliationReportV1",
    "reconcile_shadow",
]
