"""Read-only, complete-population proof for the future billing numbering seed.

No series is attached, configured, reserved, repaired or advanced by this owner.
The caller owns session lifecycle and must supply a stable read-only snapshot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from dotmac_numbering.models import AllocationReceipt, NumberSeries, SeriesCounter
from dotmac_numbering.service import MAX_DIGITS, MAX_VALUE, format_number, period_for
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import CreditNote, Invoice
from app.models.domain_settings import SettingDomain
from app.models.sequence import DocumentSequence
from app.services import settings_spec
from app.services.operator_tenant import OPERATOR_TENANT_ID

DIGEST_VERSION = "billing-numbering-readiness-v1"
SCOPE = TenantScope(tenant_id=OPERATOR_TENANT_ID)


class SeriesKind(StrEnum):
    INVOICE = "invoice_number"
    CREDIT_NOTE = "credit_note_number"


class FindingCode(StrEnum):
    UNSUPPORTED_CONFIGURATION = "unsupported_configuration"
    INVALID_COUNTER = "invalid_counter"
    INCOMPATIBLE_MODULE_CONFIGURATION = "incompatible_module_configuration"
    UNSUPPORTED_MODULE_PERIOD = "unsupported_module_period"
    ORPHAN_MODULE_COUNTER = "orphan_module_counter"
    INCOMPLETE_POPULATION = "incomplete_population"
    DUPLICATE_NUMBER = "duplicate_number"
    OTHER_GRAMMAR = "other_grammar"
    EXHAUSTED = "exhausted"
    INCONSISTENT_MODULE_HISTORY = "inconsistent_module_history"


@dataclass(frozen=True)
class NumberingReadinessQuery:
    reference_date: date


@dataclass(frozen=True)
class LegacySeriesConfiguration:
    prefix: str
    padding: int
    start_value: int


@dataclass(frozen=True)
class ModuleSeriesConfiguration:
    prefix: str
    suffix: str
    separator: str
    min_digits: int
    include_year: int
    year_digits: int
    include_month: int
    reset_policy: str
    start_value: int


@dataclass(frozen=True)
class ModuleCounter:
    period_key: str
    next_value: int


@dataclass(frozen=True)
class ModuleReceiptObservation:
    receipt_id: UUID
    period_key: str
    allocated_value: int
    formatted_number: str
    reference_date: date


@dataclass(frozen=True)
class ModuleReceiptMaximum:
    period_key: str
    allocated_value: int


@dataclass(frozen=True)
class ModuleReceiptPopulation:
    database_total: int
    scanned: int
    maximum_by_period: tuple[ModuleReceiptMaximum, ...]
    evidence_digest: str
    complete: bool


@dataclass(frozen=True)
class NumberObservation:
    document_id: UUID
    raw_number: str | None


@dataclass(frozen=True)
class PopulationCounts:
    database_total: int
    scanned: int
    null: int
    empty: int
    matching: int
    other: int
    duplicate_occurrences: int


@dataclass(frozen=True)
class Finding:
    code: FindingCode
    blocking: bool
    occurrences: int = 1


@dataclass(frozen=True)
class SeedPlan:
    kind: SeriesKind
    configuration: LegacySeriesConfiguration
    module_configuration: ModuleSeriesConfiguration | None
    module_counters: tuple[ModuleCounter, ...]
    legacy_next_value: int | None
    module_receipts: ModuleReceiptPopulation
    counts: PopulationCounts
    maximum_matching_value: int | None
    proposed_next_value: int | None
    preview_number: str | None
    period_key: str
    complete: bool
    admissible: bool
    findings: tuple[Finding, ...]
    evidence_digest: str


@dataclass(frozen=True)
class NumberingReadinessReport:
    reference_date: date
    observed_at: datetime
    tenant_id: UUID
    digest_version: str
    plans: tuple[SeedPlan, ...]
    evidence_digest: str

    @property
    def complete(self) -> bool:
        return all(plan.complete for plan in self.plans)

    @property
    def admissible(self) -> bool:
        return self.complete and all(plan.admissible for plan in self.plans)

    def as_dict(self) -> dict[str, object]:
        # Serialization is an explicit reporting boundary; no UUID/raw-number
        # population is exposed, only its versioned digest and aggregate counts.
        result = asdict(self)
        result["complete"] = self.complete
        result["admissible"] = self.admissible
        return result


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _transient_series(
    kind: SeriesKind, configuration: LegacySeriesConfiguration
) -> NumberSeries:
    return NumberSeries(
        tenant_id=SCOPE.tenant_id,
        series_code=kind.value,
        prefix=configuration.prefix,
        suffix="",
        separator="",
        min_digits=max(1, configuration.padding),
        include_year=0,
        year_digits=4,
        include_month=0,
        reset_policy="never",
        start_value=configuration.start_value,
    )


def build_seed_plan(
    *,
    query: NumberingReadinessQuery,
    kind: SeriesKind,
    configuration: LegacySeriesConfiguration,
    observations: Iterable[NumberObservation],
    database_total: int,
    legacy_next_value: int | None = None,
    module_configuration: ModuleSeriesConfiguration | None = None,
    module_counters: tuple[ModuleCounter, ...] = (),
    module_receipts: Iterable[ModuleReceiptObservation] = (),
    module_receipt_total: int = 0,
) -> SeedPlan:
    """Prove a next value from ordered full-population observations, without I/O.

    Callers must order observations by document UUID. Historical duplicate and
    foreign-grammar labels remain visible; neither can equal the proven future
    exact-grammar label above MAX. No historical document is repaired.
    """
    findings: list[Finding] = []
    supported = (
        len(configuration.prefix) <= 32
        and 0 <= configuration.padding <= MAX_DIGITS
        and 1 <= configuration.start_value < MAX_VALUE
    )
    if not supported:
        findings.append(Finding(FindingCode.UNSUPPORTED_CONFIGURATION, True))
    series = _transient_series(kind, configuration)
    period = period_for(query.reference_date, "never")
    expected = ModuleSeriesConfiguration(
        prefix=configuration.prefix,
        suffix="",
        separator="",
        min_digits=max(1, configuration.padding),
        include_year=0,
        year_digits=4,
        include_month=0,
        reset_policy="never",
        start_value=configuration.start_value,
    )
    if module_configuration is not None and module_configuration != expected:
        findings.append(Finding(FindingCode.INCOMPATIBLE_MODULE_CONFIGURATION, True))
    if module_counters and module_configuration is None:
        findings.append(Finding(FindingCode.ORPHAN_MODULE_COUNTER, True))
    if any(counter.period_key != period for counter in module_counters):
        findings.append(Finding(FindingCode.UNSUPPORTED_MODULE_PERIOD, True))
    counters = tuple(counter.next_value for counter in module_counters)
    if any(
        value < 1 or value >= MAX_VALUE
        for value in counters
        + (() if legacy_next_value is None else (legacy_next_value,))
    ):
        findings.append(Finding(FindingCode.INVALID_COUNTER, True))
    receipt_hash = hashlib.sha256()
    receipt_scanned = 0
    previous_receipt_id: UUID | None = None
    receipt_ordered_unique = True
    receipt_maxima: dict[str, int] = {}
    receipt_inconsistent = False
    counter_by_period = {
        counter.period_key: counter.next_value for counter in module_counters
    }
    for receipt in module_receipts:
        receipt_scanned += 1
        if (
            previous_receipt_id is not None
            and receipt.receipt_id.int <= previous_receipt_id.int
        ):
            receipt_ordered_unique = False
        previous_receipt_id = receipt.receipt_id
        receipt_hash.update(
            json.dumps(
                (
                    str(receipt.receipt_id),
                    receipt.period_key,
                    receipt.allocated_value,
                    receipt.formatted_number,
                    receipt.reference_date.isoformat(),
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        receipt_maxima[receipt.period_key] = max(
            receipt_maxima.get(receipt.period_key, 0), receipt.allocated_value
        )
        if (
            module_configuration is None
            or receipt.period_key != period
            or not 1 <= receipt.allocated_value < MAX_VALUE
            or counter_by_period.get(receipt.period_key, 0) <= receipt.allocated_value
        ):
            receipt_inconsistent = True
        if (
            supported
            and format_number(
                series,
                value=max(1, receipt.allocated_value),
                reference_date=receipt.reference_date,
            )
            != receipt.formatted_number
        ):
            receipt_inconsistent = True
    receipt_complete = (
        receipt_ordered_unique and receipt_scanned == module_receipt_total
    )
    receipt_population = ModuleReceiptPopulation(
        database_total=module_receipt_total,
        scanned=receipt_scanned,
        maximum_by_period=tuple(
            ModuleReceiptMaximum(period_key=key, allocated_value=value)
            for key, value in sorted(receipt_maxima.items())
        ),
        evidence_digest=receipt_hash.hexdigest(),
        complete=receipt_complete,
    )
    if receipt_inconsistent:
        findings.append(Finding(FindingCode.INCONSISTENT_MODULE_HISTORY, True))
    scanned = null = empty = matching = other = duplicates = 0
    maximum: int | None = None
    seen: dict[str, None] = {}
    row_digest = hashlib.sha256()
    previous_id: UUID | None = None
    ordered_unique = True
    for observation in observations:
        scanned += 1
        if previous_id is not None and observation.document_id.int <= previous_id.int:
            ordered_unique = False
        previous_id = observation.document_id
        raw = observation.raw_number
        row_digest.update(
            json.dumps(
                (kind.value, str(observation.document_id), raw),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if raw is None:
            null += 1
            continue
        if raw == "":
            empty += 1
            continue
        if raw in seen:
            duplicates += 1
        seen[raw] = None
        remainder = (
            raw[len(configuration.prefix) :]
            if raw.startswith(configuration.prefix)
            else ""
        )
        if (
            supported
            and remainder
            and remainder.isascii()
            and remainder.isdecimal()
            and int(remainder) > 0
        ):
            value = int(remainder)
            if (
                format_number(series, value=value, reference_date=query.reference_date)
                == raw
            ):
                matching += 1
                maximum = value if maximum is None else max(maximum, value)
                continue
        other += 1
    counts = PopulationCounts(
        database_total, scanned, null, empty, matching, other, duplicates
    )
    complete = (
        receipt_complete
        and ordered_unique
        and scanned == database_total == null + empty + matching + other
    )
    if not complete:
        findings.append(Finding(FindingCode.INCOMPLETE_POPULATION, True))
    if duplicates:
        findings.append(Finding(FindingCode.DUPLICATE_NUMBER, False, duplicates))
    if other:
        findings.append(Finding(FindingCode.OTHER_GRAMMAR, False, other))
    next_value = max(
        configuration.start_value if legacy_next_value is None else legacy_next_value,
        configuration.start_value if maximum is None else maximum + 1,
        *counters,
    )
    if next_value >= MAX_VALUE:
        findings.append(Finding(FindingCode.EXHAUSTED, True))
    admissible = complete and not any(finding.blocking for finding in findings)
    evidence = _digest(
        {
            "version": DIGEST_VERSION,
            "tenant_id": str(SCOPE.tenant_id),
            "kind": kind.value,
            "reference_date": query.reference_date.isoformat(),
            "configuration": asdict(configuration),
            "legacy_next_value": legacy_next_value,
            "module_configuration": asdict(module_configuration)
            if module_configuration
            else None,
            "module_counters": [asdict(counter) for counter in module_counters],
            "counts": asdict(counts),
            "module_receipts": asdict(receipt_population),
            "ordered_unique": ordered_unique,
            "rows_digest": row_digest.hexdigest(),
        }
    )
    return SeedPlan(
        kind,
        configuration,
        module_configuration,
        module_counters,
        legacy_next_value,
        receipt_population,
        counts,
        maximum,
        next_value if admissible else None,
        format_number(series, value=next_value, reference_date=query.reference_date)
        if admissible
        else None,
        period,
        complete,
        admissible,
        tuple(findings),
        evidence,
    )


def _configuration(db: Session, kind: SeriesKind) -> LegacySeriesConfiguration:
    prefix = settings_spec.resolve_value(
        db, SettingDomain.billing, f"{kind.value}_prefix"
    )
    padding = settings_spec.resolve_value(
        db, SettingDomain.billing, f"{kind.value}_padding"
    )
    start = settings_spec.resolve_value(
        db, SettingDomain.billing, f"{kind.value}_start"
    )
    # Canonical settings_spec owns parsing/defaulting. Unexpected effective
    # shapes are unsupported input, never an invented replacement policy.
    if (
        not isinstance(prefix, str)
        or type(padding) is not int
        or type(start) is not int
    ):
        raise NumberingReadinessError("unsupported_configuration")
    return LegacySeriesConfiguration(prefix=prefix, padding=padding, start_value=start)


class NumberingReadinessError(Exception):
    """Stable, transport-neutral failure for unavailable authoritative inputs."""

    def __init__(self, code: str) -> None:
        self.code = f"financial.billing_numbering_readiness.{code}"
        super().__init__(self.code)


def billing_numbering_readiness_report(
    db: Session, *, query: NumberingReadinessQuery
) -> NumberingReadinessReport:
    """Read each entire document population from the caller's snapshot.

    Settings cache freshness belongs to the caller: the standalone CLI disables
    its process-local cache store; this service never changes global cache state.
    """
    plans: list[SeedPlan] = []
    with db.no_autoflush:
        for kind in SeriesKind:
            configuration = _configuration(db, kind)
            model = Invoice if kind is SeriesKind.INVOICE else CreditNote
            number_column = (
                Invoice.invoice_number
                if kind is SeriesKind.INVOICE
                else CreditNote.credit_number
            )
            total = db.scalar(select(func.count()).select_from(model))
            if total is None:
                raise NumberingReadinessError("incomplete_population")
            legacy_next = db.scalar(
                select(DocumentSequence.next_value).where(
                    DocumentSequence.key == kind.value
                )
            )
            module = db.scalar(
                select(NumberSeries).where(
                    NumberSeries.tenant_id == SCOPE.tenant_id,
                    NumberSeries.series_code == kind.value,
                )
            )
            module_configuration = (
                None
                if module is None
                else ModuleSeriesConfiguration(
                    prefix=module.prefix,
                    suffix=module.suffix,
                    separator=module.separator,
                    min_digits=module.min_digits,
                    include_year=module.include_year,
                    year_digits=module.year_digits,
                    include_month=module.include_month,
                    reset_policy=module.reset_policy,
                    start_value=module.start_value,
                )
            )
            module_counters = tuple(
                ModuleCounter(period_key=row.period_key, next_value=row.next_value)
                for row in db.execute(
                    select(SeriesCounter.period_key, SeriesCounter.next_value)
                    .where(
                        SeriesCounter.tenant_id == SCOPE.tenant_id,
                        SeriesCounter.series_code == kind.value,
                    )
                    .order_by(SeriesCounter.period_key)
                )
            )
            receipt_filter = (
                AllocationReceipt.tenant_id == SCOPE.tenant_id,
                AllocationReceipt.series_code == kind.value,
            )
            receipt_total = db.scalar(
                select(func.count())
                .select_from(AllocationReceipt)
                .where(*receipt_filter)
            )
            if receipt_total is None:
                raise NumberingReadinessError("incomplete_population")
            receipt_rows = db.execute(
                select(
                    AllocationReceipt.id,
                    AllocationReceipt.period_key,
                    AllocationReceipt.allocated_value,
                    AllocationReceipt.formatted_number,
                    AllocationReceipt.reference_date,
                )
                .where(*receipt_filter)
                .order_by(AllocationReceipt.id)
                .execution_options(yield_per=1000)
            )
            receipt_observations = (
                ModuleReceiptObservation(
                    receipt_id=row[0],
                    period_key=row[1],
                    allocated_value=row[2],
                    formatted_number=row[3],
                    reference_date=row[4],
                )
                for row in receipt_rows
            )
            rows = db.execute(
                select(model.id, number_column)
                .order_by(model.id)
                .execution_options(yield_per=1000)
            )
            observations = (
                NumberObservation(document_id=row[0], raw_number=row[1]) for row in rows
            )
            plans.append(
                build_seed_plan(
                    query=query,
                    kind=kind,
                    configuration=configuration,
                    observations=observations,
                    database_total=total,
                    legacy_next_value=legacy_next,
                    module_configuration=module_configuration,
                    module_counters=module_counters,
                    module_receipts=receipt_observations,
                    module_receipt_total=receipt_total,
                )
            )
    digest = _digest(
        {
            "version": DIGEST_VERSION,
            "tenant_id": str(SCOPE.tenant_id),
            "plans": [plan.evidence_digest for plan in plans],
        }
    )
    return NumberingReadinessReport(
        query.reference_date,
        datetime.now(UTC),
        SCOPE.tenant_id,
        DIGEST_VERSION,
        tuple(plans),
        digest,
    )
