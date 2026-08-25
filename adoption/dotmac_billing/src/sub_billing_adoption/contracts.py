"""Immutable control contracts for Sub's Billing authority migration.

These contracts describe migration evidence only. Billing's own published
commands and facts remain imported from ``dotmac_billing``; this package does
not copy or fork those financial contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from dotmac_billing import DueDateBasisStatus, DueDateBasisV1

from sub_billing_adoption.errors import AdoptionErrorCode, BillingAdoptionError


class LegacyFactKind(StrEnum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    SETTLEMENT = "settlement"
    REFUND = "refund"
    REVERSAL = "reversal"
    ALLOCATION = "allocation"
    POSITION = "position"
    TAX_SNAPSHOT = "tax_snapshot"
    FX_SNAPSHOT = "fx_snapshot"


class SourceAuthority(StrEnum):
    NATIVE_INTERNAL = "native_internal"
    PROVIDER_OWNED = "provider_owned"
    LEGACY_IMPORTED = "legacy_imported"


class FactLifecycle(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class EvidenceState(StrEnum):
    COMPLETE = "complete"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class LegacyDisposition(StrEnum):
    TARGET_BACKFILL = "authoritative_active_fact_with_complete_provenance"
    PROVIDER_PROJECTION = "provider_owned_observation"
    CLOSED_LEGACY_ARCHIVE = "closed_legacy_evidence_gap"
    CUTOVER_BLOCKER = "active_open_fact_with_missing_provenance"
    KNOWN_INCORRECT_NATIVE_FACT = "known_incorrect_native_fact"


class AuthorityPhase(StrEnum):
    PRE_SWITCH = "pre_switch"
    MAINTENANCE_PAUSED = "maintenance_paused"
    POST_SWITCH = "post_switch"


class WriterState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class BillingAuthorityState(StrEnum):
    UNMOUNTED = "unmounted"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class LegacyFinancialFactV1:
    tenant_id: UUID
    fact_kind: LegacyFactKind
    fact_id: UUID
    account_id: UUID
    currency: str
    minor_units: int
    amount: Decimal
    source_authority: SourceAuthority
    lifecycle: FactLifecycle
    source_ref: str
    source_version: str | None
    observed_at: datetime
    due_date_basis: DueDateBasisV1 | None
    settlement_evidence: EvidenceState
    allocation_evidence: EvidenceState
    tax_evidence: EvidenceState
    fx_evidence: EvidenceState
    known_incorrect: bool = False

    def __post_init__(self) -> None:
        problems: list[str] = []
        if len(self.currency) != 3 or self.currency != self.currency.upper():
            problems.append("currency must be a three-letter uppercase code")
        if not 0 <= self.minor_units <= 6:
            problems.append("minor_units must be between zero and six")
        if isinstance(self.amount, float) or not isinstance(self.amount, Decimal):
            problems.append("amount must be Decimal and never float")
        elif not self.amount.is_finite():
            problems.append("amount must be finite")
        if not self.source_ref.strip():
            problems.append("source_ref is required")
        if (
            self.known_incorrect
            and self.source_authority is SourceAuthority.PROVIDER_OWNED
        ):
            problems.append(
                "provider observations cannot be classified as native repairs"
            )
        if problems:
            raise BillingAdoptionError(
                AdoptionErrorCode.INVALID_SOURCE_FACT,
                "; ".join(problems),
                context={"fact_id": str(self.fact_id)},
            )


@dataclass(frozen=True, slots=True)
class DispositionResultV1:
    fact_id: UUID
    fact_kind: LegacyFactKind
    disposition: LegacyDisposition
    missing_evidence: tuple[str, ...]
    collectible: bool
    accounting_reemit_allowed: bool
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class SourceHighWatermarkV1:
    source: str
    value: str

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.value.strip():
            raise BillingAdoptionError(
                AdoptionErrorCode.INCOHERENT_WATERMARK,
                "a source high-water mark requires source and value",
            )


@dataclass(frozen=True, slots=True)
class CoupledAuthorityWatermarkV1:
    watermark_id: UUID
    phase: AuthorityPhase
    invoice_writer: WriterState
    settlement_writer: WriterState
    allocation_writer: WriterState
    billing_authority: BillingAuthorityState
    source_marks: tuple[SourceHighWatermarkV1, ...]
    recorded_at: datetime
    first_post_watermark_fact_id: UUID | None = None

    def __post_init__(self) -> None:
        legacy_states = {
            self.invoice_writer,
            self.settlement_writer,
            self.allocation_writer,
        }
        coherent = (
            (
                self.phase is AuthorityPhase.PRE_SWITCH
                and legacy_states == {WriterState.ACTIVE}
                and self.billing_authority is BillingAuthorityState.UNMOUNTED
                and self.first_post_watermark_fact_id is None
            )
            or (
                self.phase is AuthorityPhase.MAINTENANCE_PAUSED
                and legacy_states == {WriterState.DISABLED}
                and self.billing_authority is BillingAuthorityState.UNMOUNTED
                and self.first_post_watermark_fact_id is None
                and bool(self.source_marks)
            )
            or (
                self.phase is AuthorityPhase.POST_SWITCH
                and legacy_states == {WriterState.DISABLED}
                and self.billing_authority is BillingAuthorityState.ACTIVE
                and bool(self.source_marks)
            )
        )
        if not coherent:
            raise BillingAdoptionError(
                AdoptionErrorCode.INCOHERENT_WATERMARK,
                "invoice, settlement, and allocation authority must switch together",
                context={"phase": self.phase.value},
            )
        sources = tuple(mark.source for mark in self.source_marks)
        if len(sources) != len(set(sources)):
            raise BillingAdoptionError(
                AdoptionErrorCode.INCOHERENT_WATERMARK,
                "a coupled watermark cannot repeat a source",
                context={"watermark_id": str(self.watermark_id)},
            )


def due_date_basis_is_collectible(value: DueDateBasisV1 | None) -> bool:
    return value is not None and value.status is DueDateBasisStatus.VERIFIED


__all__ = [
    "AuthorityPhase",
    "BillingAuthorityState",
    "CoupledAuthorityWatermarkV1",
    "DispositionResultV1",
    "EvidenceState",
    "FactLifecycle",
    "LegacyDisposition",
    "LegacyFactKind",
    "LegacyFinancialFactV1",
    "SourceAuthority",
    "SourceHighWatermarkV1",
    "WriterState",
    "due_date_basis_is_collectible",
]
