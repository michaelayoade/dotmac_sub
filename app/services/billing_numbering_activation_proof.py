"""Read-only invoice candidate evidence delegated by the existing readiness owner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from uuid import UUID

from dotmac_numbering import (
    MAX_DIGITS,
    MAX_VALUE,
    AllocationReceipt,
    NumberSeries,
    SeriesConfiguration,
    SeriesCounter,
    format_number,
    preview,
)
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.models.sequence import DocumentSequence
from app.services.billing_numbering_readiness import (
    SCOPE,
    LegacySeriesConfiguration,
    NumberingReadinessError,
    NumberingReadinessQuery,
    NumberObservation,
    SeriesKind,
    billing_numbering_readiness_report,
)

OWNER = "financial.billing_numbering_readiness"
DIGEST_VERSION = "billing-numbering-activation-proof-v2"
OPERATIONAL_GATES = (
    "approved_target_configuration",
    "legacy_settings_retirement",
    "incumbent_writer_drain_and_sealed_flip",
    "accepted_inverse",
    "released_kernel_bridge",
)


@dataclass(frozen=True)
class ActivationProofQuery:
    reference_date: date
    proposal: SeriesConfiguration


@dataclass(frozen=True)
class LegacyCounterObservation:
    counter_id: UUID
    key: str
    next_value: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TargetSeriesObservation:
    series_id: UUID
    tenant_id: UUID
    configuration: SeriesConfiguration
    created_at: datetime
    updated_at: datetime
    include_year_flag: int = 0
    include_month_flag: int = 0


@dataclass(frozen=True)
class TargetCounterObservation:
    counter_id: UUID
    tenant_id: UUID
    series_code: str
    period_key: str
    next_value: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TargetReceiptObservation:
    receipt_id: UUID
    tenant_id: UUID
    series_code: str
    period_key: str
    allocated_value: int
    formatted_number: str
    reference_date: date
    idempotency_key: str
    allocated_at: datetime
    allocated_by: str | None


@dataclass(frozen=True)
class ActivationProofReport:
    reference_date: date
    proposal: SeriesConfiguration
    scanned: int
    database_total: int
    null: int
    empty: int
    legacy_matches: int
    other: int
    duplicate_occurrences: int
    target_matches: int
    receipt_total: int
    receipt_scanned: int
    complete: bool
    disjoint_languages: bool
    candidate_proven: bool
    actual_preview: str | None
    actual_preview_verified: bool
    blockers: tuple[str, ...]
    evidence_digest: str
    legacy_readiness_digest: str
    proposed_next_value: int | None = None
    seeded_counter_present: bool = False
    digest_version: str = DIGEST_VERSION
    owner: str = OWNER
    activatable: bool = False
    operational_gates: tuple[str, ...] = OPERATIONAL_GATES

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _scalar(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(type(value).__name__)


def _encoded(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=_scalar
    ).encode()


def disjoint_head_witness(legacy_prefix: str, target_head: str) -> bool:
    """Sufficient language proof; failure means unproved, never sampled overlap."""
    shared = min(len(legacy_prefix), len(target_head))
    return legacy_prefix[:shared] != target_head[:shared] or (
        target_head.startswith(legacy_prefix)
        and any(
            character not in "0123456789"
            for character in target_head[len(legacy_prefix) :]
        )
    )


def _supported(proposal: SeriesConfiguration) -> bool:
    return (
        proposal.series_code == SeriesKind.INVOICE.value
        and proposal.reset_policy == "never"
        and proposal.include_year is False
        and proposal.include_month is False
        and type(proposal.min_digits) is int
        and 1 <= proposal.min_digits <= MAX_DIGITS
        and type(proposal.start_value) is int
        and 1 <= proposal.start_value < MAX_VALUE
        and type(proposal.year_digits) is int
        and proposal.year_digits in (2, 4)
        and all(
            isinstance(value, str)
            for value in (proposal.prefix, proposal.suffix, proposal.separator)
        )
        and len(proposal.prefix) <= 32
        and len(proposal.suffix) <= 32
        and len(proposal.separator) <= 8
        and len(proposal.prefix)
        + len(proposal.suffix)
        + max(proposal.min_digits, 15)
        + len(proposal.separator) * (bool(proposal.prefix) + bool(proposal.suffix))
        <= 80
    )


def _series(configuration: SeriesConfiguration) -> NumberSeries:
    return NumberSeries(tenant_id=SCOPE.tenant_id, **asdict(configuration))


def _matches(
    raw: str, configuration: SeriesConfiguration, reference_date: date
) -> bool:
    head = (
        configuration.prefix + configuration.separator if configuration.prefix else ""
    )
    tail = (
        configuration.separator + configuration.suffix if configuration.suffix else ""
    )
    if not raw.startswith(head) or not raw.endswith(tail):
        return False
    middle = raw[len(head) : len(raw) - len(tail) if tail else len(raw)]
    # Invoice storage caps labels at 80 characters, safely below Python int limits.
    # Historical values above the allocation ceiling still receive exact replay.
    if not middle or len(middle) > 80 or any(c not in "0123456789" for c in middle):
        return False
    value = int(middle)
    return (
        value > 0
        and format_number(
            _series(configuration), value=value, reference_date=reference_date
        )
        == raw
    )


def build_activation_proof(
    *,
    query: ActivationProofQuery,
    legacy_configuration: LegacySeriesConfiguration,
    observations: Iterable[NumberObservation],
    database_total: int,
    legacy_next_value: int | None = None,
    legacy_counter: LegacyCounterObservation | None = None,
    target_series: TargetSeriesObservation | None = None,
    counters: tuple[TargetCounterObservation, ...] = (),
    receipts: Iterable[TargetReceiptObservation] = (),
    receipt_total: int = 0,
    actual_preview: str | None = None,
    legacy_readiness_digest: str = "",
) -> ActivationProofReport:
    """Pure complete-population proof, separate from preparation and activation."""
    proposal = query.proposal
    supported = _supported(proposal)
    legacy_supported = (
        isinstance(legacy_configuration.prefix, str)
        and type(legacy_configuration.padding) is int
        and 0 <= legacy_configuration.padding <= MAX_DIGITS
        and type(legacy_configuration.start_value) is int
        and 1 <= legacy_configuration.start_value < MAX_VALUE
    )
    legacy = SeriesConfiguration(
        series_code="invoice_number",
        prefix=legacy_configuration.prefix,
        suffix="",
        separator="",
        min_digits=max(1, legacy_configuration.padding),
        include_year=False,
        year_digits=4,
        include_month=False,
        reset_policy="never",
        start_value=legacy_configuration.start_value,
    )
    head = proposal.prefix + proposal.separator if proposal.prefix else ""
    disjoint = (
        supported
        and legacy_supported
        and disjoint_head_witness(legacy_configuration.prefix, head)
    )
    blockers: list[str] = []
    if not supported or not legacy_supported:
        blockers.append("unsupported_proposal")
    if not disjoint:
        blockers.append("unproved_language_disjointness")
    legacy_maximum = 0
    scanned = null = empty = legacy_matches = other = duplicates = target_matches = 0
    ordered = True
    previous: UUID | None = None
    seen: set[str] = set()
    row_hash = hashlib.sha256()
    for row in observations:
        scanned += 1
        ordered = ordered and (previous is None or row.document_id.int > previous.int)
        previous = row.document_id
        row_hash.update(_encoded((str(row.document_id), row.raw_number)) + b"\n")
        raw = row.raw_number
        if raw is None:
            null += 1
        elif raw == "":
            empty += 1
        else:
            duplicates += int(raw in seen)
            seen.add(raw)
            target_matches += int(
                supported and _matches(raw, proposal, query.reference_date)
            )
            if legacy_supported and _matches(raw, legacy, query.reference_date):
                legacy_matches += 1
                legacy_maximum = max(
                    legacy_maximum, int(raw[len(legacy_configuration.prefix) :])
                )
            else:
                other += 1
    if target_matches:
        blockers.append("historical_target_collision")
    receipt_hash = hashlib.sha256()
    receipt_scanned = 0
    previous = None
    receipt_ordered = True
    receipt_inconsistent = False
    for receipt in receipts:
        receipt_scanned += 1
        receipt_ordered = receipt_ordered and (
            previous is None or receipt.receipt_id.int > previous.int
        )
        previous = receipt.receipt_id
        receipt_hash.update(_encoded(asdict(receipt)) + b"\n")
        receipt_inconsistent = receipt_inconsistent or not (
            supported
            and receipt.tenant_id == SCOPE.tenant_id
            and receipt.series_code == proposal.series_code
            and receipt.period_key == "*"
            and 1 <= receipt.allocated_value < MAX_VALUE
            and receipt.formatted_number
            == format_number(
                _series(proposal),
                value=max(1, receipt.allocated_value),
                reference_date=receipt.reference_date,
            )
            and len(counters) == 1
            and counters[0].next_value > receipt.allocated_value
            and bool(receipt.idempotency_key)
        )
    if receipt_inconsistent:
        blockers.append("inconsistent_target_receipts")
    if receipt_scanned or receipt_total:
        blockers.append("existing_target_history_requires_reconciliation")
    complete = (
        ordered
        and receipt_ordered
        and scanned == database_total == null + empty + legacy_matches + other
        and receipt_scanned == receipt_total
    )
    if not complete:
        blockers.append("incomplete_population")
    if target_series is None:
        blockers.append("missing_target_configuration")
    elif (
        target_series.configuration != proposal
        or target_series.tenant_id != SCOPE.tenant_id
        or target_series.include_year_flag != 0
        or target_series.include_month_flag != 0
    ):
        blockers.append("target_configuration_mismatch")
    counter_valid = (
        len(counters) == 1
        and counters[0].period_key == "*"
        and counters[0].tenant_id == SCOPE.tenant_id
        and counters[0].series_code == proposal.series_code
        and type(counters[0].next_value) is int
        and proposal.start_value <= counters[0].next_value < MAX_VALUE
    )
    if not counter_valid:
        blockers.append("missing_or_invalid_seeded_counter")
    proposed_next = max(
        proposal.start_value,
        legacy_configuration.start_value,
        legacy_maximum + 1,
        legacy_next_value or 1,
        *(counter.next_value for counter in counters),
    )
    if legacy_next_value is not None and (
        type(legacy_next_value) is not int or not 1 <= legacy_next_value < MAX_VALUE
    ):
        blockers.append("invalid_legacy_counter")
    if proposed_next >= MAX_VALUE:
        blockers.append("exhausted_proposed_counter")
    if counter_valid and counters[0].next_value != proposed_next:
        blockers.append("insufficient_seeded_counter")
    preview_verified = bool(
        supported
        and target_series is not None
        and target_series.configuration == proposal
        and target_series.tenant_id == SCOPE.tenant_id
        and target_series.include_year_flag == target_series.include_month_flag == 0
        and counter_valid
        and counters[0].next_value == proposed_next
        and actual_preview
        == format_number(
            _series(proposal),
            value=counters[0].next_value,
            reference_date=query.reference_date,
        )
    )
    if not preview_verified:
        blockers.append("actual_public_preview_unmet")
    candidate_proven = (
        complete
        and disjoint
        and not target_matches
        and receipt_scanned == receipt_total == 0
    )
    evidence = {
        "version": DIGEST_VERSION,
        "tenant": SCOPE.tenant_id,
        "query": asdict(query),
        "legacy_configuration": asdict(legacy_configuration),
        "legacy_next_value": legacy_next_value,
        "legacy_counter": asdict(legacy_counter) if legacy_counter else None,
        "legacy_readiness_digest": legacy_readiness_digest,
        "target_series": asdict(target_series) if target_series else None,
        "counters": [asdict(counter) for counter in counters],
        "receipt_total": receipt_total,
        "receipt_scanned": receipt_scanned,
        "receipt_digest": receipt_hash.hexdigest(),
        "rows_digest": row_hash.hexdigest(),
        "database_total": database_total,
        "scanned": scanned,
        "ordered": ordered,
        "receipt_ordered": receipt_ordered,
        "actual_preview": actual_preview,
        "blockers": blockers,
    }
    return ActivationProofReport(
        query.reference_date,
        proposal,
        scanned,
        database_total,
        null,
        empty,
        legacy_matches,
        other,
        duplicates,
        target_matches,
        receipt_total,
        receipt_scanned,
        complete,
        disjoint,
        candidate_proven,
        actual_preview,
        preview_verified,
        tuple(blockers),
        hashlib.sha256(_encoded(evidence)).hexdigest(),
        legacy_readiness_digest,
        proposed_next_value=proposed_next
        if supported and proposed_next < MAX_VALUE
        else None,
        seeded_counter_present=counter_valid,
    )


def billing_numbering_activation_proof_report(
    db: Session, *, query: ActivationProofQuery
) -> ActivationProofReport:
    """Observe only a caller-owned clean PostgreSQL read-only stable snapshot."""
    if db.new or db.dirty or db.deleted or db.get_bind().dialect.name != "postgresql":
        raise NumberingReadinessError("unsafe_snapshot")
    with db.no_autoflush:
        isolation = db.scalar(text("SHOW transaction_isolation"))
        readonly = db.scalar(text("SHOW transaction_read_only"))
        if isolation not in ("repeatable read", "serializable") or readonly != "on":
            raise NumberingReadinessError("unsafe_snapshot")
        # A clean reused Session can retain mapped values from an earlier
        # transaction. Refresh every observation, including the delegated V1
        # census and released preview, from this verified database snapshot.
        db.expire_all()
        legacy_report = billing_numbering_readiness_report(
            db, query=NumberingReadinessQuery(query.reference_date)
        )
        legacy = next(
            plan for plan in legacy_report.plans if plan.kind is SeriesKind.INVOICE
        )
        incumbent = db.scalar(
            select(DocumentSequence).where(
                DocumentSequence.key == SeriesKind.INVOICE.value
            )
        )
        incumbent_observation = (
            None
            if incumbent is None
            else LegacyCounterObservation(
                incumbent.id,
                incumbent.key,
                incumbent.next_value,
                incumbent.created_at,
                incumbent.updated_at,
            )
        )
        filters = (
            NumberSeries.tenant_id == SCOPE.tenant_id,
            NumberSeries.series_code == query.proposal.series_code,
        )
        row = db.scalar(select(NumberSeries).where(*filters))
        target = (
            None
            if row is None
            else TargetSeriesObservation(
                row.id,
                row.tenant_id,
                SeriesConfiguration(
                    series_code=row.series_code,
                    prefix=row.prefix,
                    suffix=row.suffix,
                    separator=row.separator,
                    min_digits=row.min_digits,
                    include_year=bool(row.include_year),
                    year_digits=row.year_digits,
                    include_month=bool(row.include_month),
                    reset_policy=row.reset_policy,
                    start_value=row.start_value,
                ),
                row.created_at,
                row.updated_at,
                include_year_flag=row.include_year,
                include_month_flag=row.include_month,
            )
        )
        counters = tuple(
            TargetCounterObservation(
                counter.id,
                counter.tenant_id,
                counter.series_code,
                counter.period_key,
                counter.next_value,
                counter.created_at,
                counter.updated_at,
            )
            for counter in db.scalars(
                select(SeriesCounter)
                .where(
                    SeriesCounter.tenant_id == SCOPE.tenant_id,
                    SeriesCounter.series_code == query.proposal.series_code,
                )
                .order_by(SeriesCounter.id)
            )
        )
        receipt_filters = (
            AllocationReceipt.tenant_id == SCOPE.tenant_id,
            AllocationReceipt.series_code == query.proposal.series_code,
        )
        receipt_total = db.scalar(
            select(func.count()).select_from(AllocationReceipt).where(*receipt_filters)
        )
        total = db.scalar(select(func.count()).select_from(Invoice))
        if total is None or receipt_total is None:
            raise NumberingReadinessError("incomplete_population")
        receipts = (
            TargetReceiptObservation(
                receipt.id,
                receipt.tenant_id,
                receipt.series_code,
                receipt.period_key,
                receipt.allocated_value,
                receipt.formatted_number,
                receipt.reference_date,
                receipt.idempotency_key,
                receipt.allocated_at,
                receipt.allocated_by,
            )
            for receipt in db.scalars(
                select(AllocationReceipt)
                .where(*receipt_filters)
                .order_by(AllocationReceipt.id)
                .execution_options(yield_per=1000)
            )
        )
        observations = (
            NumberObservation(document_id=row[0], raw_number=row[1])
            for row in db.execute(
                select(Invoice.id, Invoice.invoice_number)
                .order_by(Invoice.id)
                .execution_options(yield_per=1000)
            )
        )
        actual_preview = None
        if (
            _supported(query.proposal)
            and target is not None
            and target.configuration == query.proposal
            and len(counters) == 1
            and counters[0].period_key == "*"
            and 1 <= counters[0].next_value < MAX_VALUE
        ):
            actual_preview = preview(
                db,
                scope=SCOPE,
                series_code=query.proposal.series_code,
                reference_date=query.reference_date,
            )
        return build_activation_proof(
            query=query,
            legacy_configuration=legacy.configuration,
            observations=observations,
            database_total=total,
            legacy_next_value=legacy.legacy_next_value,
            legacy_counter=incumbent_observation,
            target_series=target,
            counters=counters,
            receipts=receipts,
            receipt_total=receipt_total,
            actual_preview=actual_preview,
            legacy_readiness_digest=legacy_report.evidence_digest,
        )
