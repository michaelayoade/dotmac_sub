#!/usr/bin/env python3
"""Read-only Sub -> Starter opening-rehearsal adapter: a blocker census only.

This unpublished assembly tool mechanically translates Sub's serialized
opening census (``sub-billing-opening-census-v2``/``-v3`` JSON) into Starter's
typed rehearsal boundary (``OpeningSubObservationV2`` plus a candidate
``OpeningManifestV2``), runs the pure preflights and emits a counted blocker
census. It opens no database session on either side, calls no writer and
constructs no verifier. It never qualifies cash, never fabricates an original
document and computes no tax. An optional aligned evidence sidecar preserves
explicit typed originals and historical discharges without qualifying them.
``cash_lots=()`` always.
VAT belongs only to sales and is owned by dotmac-tax contracts, while source
payments remain payments. Fields that cannot be sourced mechanically become
counted blockers, never synthesized values.

The adapter refuses to run without an explicit assembly file. The production
assembly (scope, authority and mapping-version strings, partition/invoice/
discharge mappings, synthetic pristine targets, provenance) is a separate
Michael-owned signed artifact; this repository ships a fixture assembly for
tests only, under ``tests/fixtures/opening_rehearsal/``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final
from uuid import UUID

from dotmac_billing.contracts import (
    DueDateBasisV1,
    ServicePeriodEvidenceV1,
    ServicePeriodStatus,
)
from dotmac_billing.opening_contracts import (
    OpeningBlockerV1,
    OpeningCaptureV1,
    OpeningDispositionV1,
    OpeningEvidenceError,
    OpeningLaneControlsV1,
    OpeningPartitionV1,
    OpeningPopulationCountsV1,
    OpeningSourceIdentityV1,
    OpeningTargetV1,
)
from dotmac_billing.opening_receivable_contracts import (
    ImportOpeningAccountV2,
    OpeningCollectionEvidenceV1,
    OpeningHistoricalDischargeV1,
    OpeningManifestV2,
    OpeningOriginalDocumentV1,
    OpeningReceivableItemV1,
)
from dotmac_billing.opening_receivable_validation import (
    opening_account_import_blockers_v2,
    opening_manifest_fingerprint_v2,
    preview_opening_account_import_v2,
)
from dotmac_billing.opening_rehearsal import (
    opening_rehearsal_assembly_digest,
    parse_opening_rehearsal_assembly,
    parse_opening_rehearsal_json,
    parse_opening_rehearsal_original_evidence,
    render_opening_rehearsal_census,
    render_opening_rehearsal_refusal,
    report_opening_rehearsal,
)
from dotmac_billing.opening_rehearsal_contracts import (
    OpeningObservedControlV1,
    OpeningObservedInvoiceV1,
    OpeningObservedLaneV1,
    OpeningObservedPopulationV1,
    OpeningObservedPrecisionV1,
    OpeningObservedSettlementV1,
    OpeningRehearsalAssemblyPartitionV1,
    OpeningRehearsalAssemblyV1,
    OpeningRehearsalBindingV1,
    OpeningRehearsalCensusBlockerV1,
    OpeningRehearsalCensusCountV1,
    OpeningRehearsalCensusDeltaV1,
    OpeningRehearsalCensusPartitionV1,
    OpeningRehearsalCensusProvenanceV1,
    OpeningRehearsalCensusRollupV1,
    OpeningRehearsalCensusSidecarV1,
    OpeningRehearsalCensusUnmappedV1,
    OpeningRehearsalCensusV1,
    OpeningRehearsalDischargeMappingV1,
    OpeningRehearsalInvoiceMappingV1,
    OpeningRehearsalMappingV1,
    OpeningRehearsalOriginalEvidenceV1,
    OpeningRehearsalRunV1,
    OpeningSubObservationV2,
)
from dotmac_kernel.cache import Scope

__all__ = [
    "AdapterInputError",
    "AssemblyInputs",
    "V3Sidecar",
    "assemble_rehearsal_run",
    "load_assembly",
    "load_original_evidence",
    "load_sub_observation",
    "main",
    "rehearsal_census",
]

_ARTIFACT_SCHEMA: Final = "sub-starter-opening-rehearsal-census/v1"
_POPULATION_VERSIONS = frozenset(
    {"sub-billing-opening-census-v2", "sub-billing-opening-census-v3"}
)
# Starter cash table -> Sub cash_evidence row-list field (counts/IDs only).
_CASH_ROW_FIELDS = {
    "payments": "payments",
    "payment_settlements": "payment_settlements",
    "payment_provider_events": "provider_events",
    "integration_inbox": "inbox_receipts",
    "payment_refunds": "refunds",
    "payment_reversals": "reversals",
    "payment_proofs": "proofs",
    "payment_proof_corrections": "proof_corrections",
    "topup_intents": "topups",
}
_PROJECTED_INVOICE_FIELDS = frozenset(
    {
        "invoice_id",
        "account_id",
        "currency",
        "precision",
        "invoice_number",
        "issued_at",
        "total",
        "canonical_discharge",
        "residual",
        "settlement_ids",
    }
)
_PROJECTED_SETTLEMENT_FIELDS = frozenset(
    {"kind", "id", "invoice_id", "source_id", "amount", "active"}
)


class AdapterInputError(ValueError):
    """Malformed census or assembly input; only the sanitised code is shown."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class UnavailableOriginalEvidenceRecord:
    invoice_id: UUID
    requirements: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class V3Sidecar:
    """Source data the closed V2 projection cannot carry, retained verbatim.

    ``OpeningSubObservationV2`` has no field for Sub's exclusion list or its
    blocking-exception invoice identities, yet dispositions need both, so the
    loader retains them here for v2 and v3 alike. V3 additionally retains the
    typed unavailable-original-evidence records; V3 current subtotal/tax
    observations are never retained (current is not original).
    """

    exclusions: tuple[tuple[UUID, str], ...] = ()
    blocking_exceptions: tuple[tuple[UUID, str], ...] = ()
    unavailable_original_evidence: tuple[UnavailableOriginalEvidenceRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class AssemblyInputs:
    """The package-owned declaration plus the views derived from it."""

    declaration: OpeningRehearsalAssemblyV1

    @property
    def scope(self) -> Scope:
        return self.declaration.scope

    @property
    def source_system(self) -> str:
        return self.declaration.source_system

    @property
    def recorded_cutoff(self) -> datetime:
        return self.declaration.recorded_cutoff

    @property
    def source_authority_ref(self) -> str:
        return self.declaration.source_authority_ref

    @property
    def account_mapping_version(self) -> str:
        return self.declaration.account_mapping_version

    @property
    def cash_mapping_version(self) -> str:
        return self.declaration.cash_mapping_version

    @property
    def receivable_mapping_version(self) -> str:
        return self.declaration.receivable_mapping_version

    @property
    def tax_mapping_version(self) -> str:
        return self.declaration.tax_mapping_version

    @property
    def source_resolver_version(self) -> str:
        return self.declaration.source_resolver_version

    @property
    def collection_authority(self) -> str:
        return self.declaration.collection_authority

    @property
    def collection_mapping_version(self) -> str:
        return self.declaration.collection_mapping_version

    @property
    def migration_authority_ref(self) -> str:
        return self.declaration.migration_authority_ref

    @property
    def binding(self) -> OpeningRehearsalBindingV1:
        return self.declaration.binding

    @property
    def invoice_mappings(self) -> tuple[OpeningRehearsalInvoiceMappingV1, ...]:
        return self.declaration.invoice_mappings

    @property
    def discharge_mappings(self) -> tuple[OpeningRehearsalDischargeMappingV1, ...]:
        return self.declaration.discharge_mappings

    @property
    def occurred_at(self) -> datetime:
        return self.declaration.occurred_at

    @property
    def starter_head_sha(self) -> str:
        return self.declaration.starter_head_sha

    @property
    def sub_exit_code(self) -> int:
        return self.declaration.sub_exit_code

    @property
    def accepted_manual_holds(self) -> tuple[str, ...]:
        return self.declaration.accepted_manual_holds

    @property
    def assembly_digest(self) -> str:
        return opening_rehearsal_assembly_digest(self.declaration)

    @property
    def partitions(self) -> tuple[OpeningRehearsalMappingV1, ...]:
        return tuple(
            OpeningRehearsalMappingV1(row.source_account_id, _partition(row))
            for row in self.declaration.partitions
        )

    @property
    def targets(self) -> tuple[tuple[OpeningPartitionV1, OpeningTargetV1], ...]:
        return tuple(
            (
                _partition(row),
                OpeningTargetV1(
                    self.declaration.scope,
                    row.account_id,
                    row.currency,
                    row.precision,
                    row.expected_position_fingerprint,
                    row.posting_watermark,
                ),
            )
            for row in self.declaration.partitions
        )

    @property
    def idempotency_keys(self) -> tuple[tuple[OpeningPartitionV1, str], ...]:
        return tuple(
            (_partition(row), row.idempotency_key)
            for row in self.declaration.partitions
        )


def _partition(row: OpeningRehearsalAssemblyPartitionV1) -> OpeningPartitionV1:
    return OpeningPartitionV1(
        row.source_account_key, row.account_id, row.currency, row.precision
    )


def _malformed(code: str = "malformed_input") -> AdapterInputError:
    return AdapterInputError(code)


def _mapping(value: object, code: str = "malformed_input") -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _malformed(code)
    return value


def _field(
    payload: Mapping[str, object], key: str, code: str = "malformed_input"
) -> object:
    if key not in payload:
        raise _malformed(code)
    return payload[key]


def _text(value: object, code: str = "malformed_input") -> str:
    if type(value) is not str:
        raise _malformed(code)
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _boolean(value: object, code: str = "malformed_input") -> bool:
    if type(value) is not bool:
        raise _malformed(code)
    return value


def _integer(value: object, code: str = "malformed_input") -> int:
    if type(value) is not int:
        raise _malformed(code)
    return value


def _optional_integer(value: object, code: str = "malformed_input") -> int | None:
    if value is None:
        return None
    return _integer(value, code)


def _uuid(value: object, code: str = "malformed_input") -> UUID:
    if type(value) is not str:
        raise _malformed(code)
    try:
        return UUID(value)
    except ValueError:
        raise _malformed(code) from None


def _optional_uuid(value: object, code: str = "malformed_input") -> UUID | None:
    if value is None:
        return None
    return _uuid(value, code)


def _instant(value: object, code: str = "malformed_input") -> datetime:
    if type(value) is not str:
        raise _malformed(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _malformed(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _malformed(code)
    return parsed


def _optional_instant(value: object, code: str = "malformed_input") -> datetime | None:
    if value is None:
        return None
    return _instant(value, code)


def _decimal(value: object, code: str = "malformed_input") -> Decimal:
    # Money arrives as an exact Decimal string only; a JSON number (float or
    # int) is refused rather than coerced, and non-finite values are refused.
    if type(value) is not str:
        raise _malformed(code)
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise _malformed(code) from None
    if not parsed.is_finite():
        raise _malformed(code)
    return parsed


def _sequence(value: object, code: str = "malformed_input") -> Sequence[object]:
    if type(value) not in (list, tuple):
        raise _malformed(code)
    return value


def _minor_units(amount: Decimal, precision: int | None) -> int | None:
    """Exact Decimal -> minor scaling; ``None`` when it would need rounding."""
    if precision is None or not 0 <= precision <= 6 or not amount.is_finite():
        return None
    if amount.adjusted() >= 14:
        return None
    sign, digits, exponent = amount.as_tuple()
    if not isinstance(exponent, int):
        return None
    coefficient = int("".join(str(digit) for digit in digits) or "0")
    if coefficient == 0:
        return 0
    shift = exponent + precision
    if shift >= 0:
        coefficient *= 10**shift
    else:
        scale = 10 ** (-shift)
        if coefficient % scale:
            return None
        coefficient //= scale
    return -coefficient if sign else coefficient


def load_sub_observation(
    payload: Mapping[str, object],
) -> tuple[OpeningSubObservationV2, V3Sidecar]:
    """Project a Sub v2/v3 census document onto the Starter V2 observation.

    Renames and retypes only. V3 is projected down to the V2 core with its
    unavailable-original-evidence records carried in the returned sidecar;
    V3 current document observations are dropped (listed, never used as
    originals) by :func:`rehearsal_census`.
    """
    root = _mapping(payload)
    capture = _mapping(_field(root, "capture"))
    declared = _mapping(_field(capture, "declared_source"))
    population_version = _text(_field(declared, "population_version"))
    if population_version not in _POPULATION_VERSIONS:
        raise _malformed("unsupported_population_version")
    precisions = []
    for entry in _sequence(_field(declared, "currency_precisions")):
        pair = _sequence(entry)
        if len(pair) != 2:
            raise _malformed()
        precisions.append(OpeningObservedPrecisionV1(_text(pair[0]), _integer(pair[1])))
    invoices = tuple(
        OpeningObservedInvoiceV1(
            _uuid(_field(row, "invoice_id")),
            _uuid(_field(row, "account_id")),
            _text(_field(row, "currency")),
            _optional_integer(_field(row, "precision")),
            _optional_text(_field(row, "invoice_number")),
            _optional_instant(_field(row, "issued_at")),
            _decimal(_field(row, "total")),
            _decimal(_field(row, "canonical_discharge")),
            _decimal(_field(row, "residual")),
            tuple(_uuid(item) for item in _sequence(_field(row, "settlement_ids"))),
        )
        for row in (_mapping(item) for item in _sequence(_field(root, "invoices")))
    )
    settlements = tuple(
        OpeningObservedSettlementV1(
            _text(_field(row, "kind")),
            _uuid(_field(row, "id")),
            _uuid(_field(row, "invoice_id")),
            _uuid(_field(row, "source_id")),
            _decimal(_field(row, "amount")),
            _boolean(_field(row, "active")),
        )
        for row in (_mapping(item) for item in _sequence(_field(root, "settlements")))
    )
    controls = tuple(
        OpeningObservedControlV1(
            _optional_uuid(_field(row, "account_id")),
            _optional_text(_field(row, "currency")),
            _integer(_field(row, "invoice_count")),
            _integer(_field(row, "eligible_count")),
            _integer(_field(row, "excluded_count")),
            _integer(_field(row, "exception_count")),
            _decimal(_field(row, "original_total")),
            _decimal(_field(row, "canonical_discharge")),
            _decimal(_field(row, "residual")),
            _decimal(_field(row, "eligible_residual")),
            _integer(_field(row, "source_count")),
        )
        for row in (_mapping(item) for item in _sequence(_field(root, "controls")))
    )
    lanes = tuple(
        OpeningObservedLaneV1(
            _uuid(_field(row, "account_id")),
            _text(_field(row, "currency")),
            _text(_field(row, "fingerprint")),
            _text(_field(row, "authority")),
            _optional_uuid(_field(row, "authority_cutover_id")),
            _integer(_field(row, "posting_group_count")),
            _decimal(_field(row, "collectible_receivable")),
            _decimal(_field(row, "unapplied_customer_credit")),
            _decimal(_field(row, "prepaid_funding_reserved")),
            _decimal(_field(row, "prepaid_funding_consumed")),
            _decimal(_field(row, "written_off_total")),
            _decimal(_field(row, "refunded_total")),
            _decimal(_field(row, "adjustment_total")),
        )
        for row in (_mapping(item) for item in _sequence(_field(root, "lanes")))
    )
    cash = _mapping(_field(root, "cash_evidence"))
    populations = []
    for row in (_mapping(item) for item in _sequence(_field(cash, "source_counts"))):
        table = _text(_field(row, "table"))
        identities: tuple[UUID, ...] = ()
        row_field = _CASH_ROW_FIELDS.get(table)
        if row_field is not None:
            identities = tuple(
                _uuid(_field(entry, "id"))
                for entry in (
                    _mapping(item) for item in _sequence(_field(cash, row_field))
                )
            )
        populations.append(
            OpeningObservedPopulationV1(
                table, _integer(_field(row, "count")), identities
            )
        )

    def exceptions(container: Mapping[str, object]) -> tuple[tuple[str, UUID], ...]:
        return tuple(
            (_text(_field(row, "code")), _uuid(_field(row, "source_id")))
            for row in (
                _mapping(item)
                for item in _sequence(_field(container, "blocking_exceptions"))
            )
        )

    all_exceptions = exceptions(root) + exceptions(cash)
    grouped: dict[str, list[str]] = {}
    for code, source_id in all_exceptions:
        grouped.setdefault(code, []).append(str(source_id))
    blockers = tuple(
        OpeningBlockerV1(code, len(refs), tuple(refs))
        for code, refs in sorted(grouped.items())
    )
    exclusions = tuple(
        (_uuid(_field(row, "source_id")), _text(_field(row, "reason")))
        for row in (_mapping(item) for item in _sequence(_field(root, "exclusions")))
    )
    unavailable: tuple[UnavailableOriginalEvidenceRecord, ...] = ()
    if population_version == "sub-billing-opening-census-v3":
        documentary = _mapping(_field(root, "documentary_observations"))
        unavailable = tuple(
            UnavailableOriginalEvidenceRecord(
                _uuid(_field(row, "invoice_id")),
                tuple(_text(item) for item in _sequence(_field(row, "requirements"))),
                tuple(_text(item) for item in _sequence(_field(row, "reasons"))),
            )
            for row in (
                _mapping(item)
                for item in _sequence(
                    _field(documentary, "unavailable_original_evidence")
                )
            )
        )
    try:
        observation = OpeningSubObservationV2(
            _text(_field(declared, "source_repository_sha")),
            _uuid(_field(declared, "installation_id")),
            tuple(_text(item) for item in _sequence(_field(capture, "schema_heads"))),
            _text(_field(capture, "snapshot_id")),
            _instant(_field(capture, "captured_at")),
            _instant(_field(declared, "economic_cutoff")),
            _text(_field(capture, "isolation")),
            population_version,
            _boolean(_field(capture, "population_complete")),
            tuple(precisions),
            _text(_field(root, "content_fingerprint")),
            tuple(row.invoice_id for row in invoices),
            tuple(
                _uuid(item) for item in _sequence(_field(root, "eligible_invoice_ids"))
            ),
            controls,
            lanes,
            tuple(populations),
            tuple(
                _uuid(item) for item in _sequence(_field(cash, "qualified_payment_ids"))
            ),
            blockers,
            invoices,
            settlements,
        )
    except OpeningEvidenceError:
        raise _malformed() from None
    sidecar = V3Sidecar(
        exclusions=exclusions,
        blocking_exceptions=tuple(
            (source_id, code) for code, source_id in all_exceptions
        ),
        unavailable_original_evidence=unavailable,
    )
    return observation, sidecar


def load_assembly(payload: Mapping[str, object]) -> AssemblyInputs:
    """Parse the explicit assembly declaration via the package; nothing defaults."""
    try:
        return AssemblyInputs(parse_opening_rehearsal_assembly(payload))
    except OpeningEvidenceError:
        raise _malformed("malformed_assembly") from None


@dataclass(frozen=True, slots=True)
class _AssemblyOutcome:
    run: OpeningRehearsalRunV1
    adapter_blockers: tuple[tuple[str, str, str | None], ...]
    unmapped: Mapping[str, tuple[str, ...]]
    raw_blockers: tuple[tuple[OpeningPartitionV1, tuple[OpeningBlockerV1, ...]], ...]


def _collection_evidence(
    assembly: AssemblyInputs, pointer: str
) -> OpeningCollectionEvidenceV1:
    # The only honest form available from today's evidence: unknown and
    # unverified, with a source-row pointer labelled as a pointer, not proof.
    return OpeningCollectionEvidenceV1(
        "unknown_unverified",
        None,
        None,
        None,
        ServicePeriodEvidenceV1(ServicePeriodStatus.UNKNOWN_UNVERIFIED),
        None,
        DueDateBasisV1.unknown_unverified(
            source_authority=assembly.collection_authority, evidence_ref=pointer
        ),
        assembly.collection_authority,
        pointer,
        assembly.collection_mapping_version,
    )


def _assemble(
    obs: OpeningSubObservationV2,
    sidecar: V3Sidecar,
    assembly: AssemblyInputs,
    original_documents: tuple[tuple[UUID, OpeningOriginalDocumentV1], ...] = (),
    historical_discharges: tuple[
        tuple[UUID, tuple[OpeningHistoricalDischargeV1, ...]], ...
    ] = (),
) -> _AssemblyOutcome:
    precisions = {row.currency: row.precision for row in obs.currency_precisions}
    partition_by_source = {
        (mapping.source_account_id, mapping.partition.currency): mapping.partition
        for mapping in assembly.partitions
    }
    targets = dict(assembly.targets)
    idempotency_keys = dict(assembly.idempotency_keys)
    invoice_mappings: dict[UUID, OpeningRehearsalInvoiceMappingV1] = {}
    for mapping in assembly.invoice_mappings:
        invoice_mappings.setdefault(mapping.source_invoice_id, mapping)
    eligible_ids = set(obs.eligible_invoice_ids)
    exclusion_reasons: dict[UUID, str] = {}
    for source_id, reason in sidecar.exclusions:
        exclusion_reasons.setdefault(source_id, reason)
    blocking_codes: dict[UUID, list[str]] = {}
    for source_id, code in sidecar.blocking_exceptions:
        blocking_codes.setdefault(source_id, []).append(code)
    # The public source export supplies no originals. Accept only explicit typed
    # inputs, bound to observed eligible/mapped invoices; never infer documentary
    # history from mutable observations or treat this mapping as qualification.
    originals: dict[UUID, OpeningOriginalDocumentV1] = {}
    invoices = {row.invoice_id: row for row in obs.invoices}
    if type(original_documents) is not tuple:
        raise _malformed("original_document_mapping_invalid")
    for entry in original_documents:
        if type(entry) is not tuple or len(entry) != 2:
            raise _malformed("original_document_mapping_invalid")
        invoice_id, original = entry
        if (
            type(invoice_id) is not UUID
            or type(original) is not OpeningOriginalDocumentV1
        ):
            raise _malformed("original_document_mapping_invalid")
        invoice = invoices.get(invoice_id)
        if (
            invoice_id in originals
            or invoice is None
            or sum(row.invoice_id == invoice_id for row in obs.invoices) != 1
            or invoice_id not in eligible_ids
            or invoice_id not in invoice_mappings
            or invoice_id in exclusion_reasons
            or invoice_id in blocking_codes
            or (invoice.account_id, invoice.currency) not in partition_by_source
            or original.document_number != invoice.invoice_number
            or original.issued_at != invoice.issued_at
            or original.grand_total.amount != invoice.total
            or original.grand_total.currency.code != invoice.currency
            or original.grand_total.currency.minor_units
            != precisions.get(invoice.currency)
        ):
            raise _malformed("original_document_mapping_invalid")
        originals[invoice_id] = original
    # Only explicit observed settlement mappings may carry historical effects.
    # Parent namespaces/live identities remain supplied evidence, for the
    # independent verifier to qualify; no source namespace is invented here.
    histories: dict[UUID, tuple[OpeningHistoricalDischargeV1, ...]] = {}
    used_references: set[str] = set()
    if type(historical_discharges) is not tuple:
        raise _malformed("historical_discharge_mapping_invalid")
    for entry in historical_discharges:
        if type(entry) is not tuple or len(entry) != 2:
            raise _malformed("historical_discharge_mapping_invalid")
        invoice_id, effects = entry
        invoice = invoices.get(invoice_id) if type(invoice_id) is UUID else None
        if (
            invoice is None
            or invoice_id in histories
            or invoice_id not in originals
            or type(effects) is not tuple
        ):
            raise _malformed("historical_discharge_mapping_invalid")
        for effect in effects:
            if type(effect) is not OpeningHistoricalDischargeV1:
                raise _malformed("historical_discharge_mapping_invalid")
            mappings = [
                row
                for row in assembly.discharge_mappings
                if row.historical_reference == effect.reference
            ]
            if len(mappings) != 1 or effect.reference in used_references:
                raise _malformed("historical_discharge_mapping_invalid")
            mapping = mappings[0]
            if (
                sum(
                    (row.settlement_kind, row.settlement_id)
                    == (mapping.settlement_kind, mapping.settlement_id)
                    for row in assembly.discharge_mappings
                )
                != 1
            ):
                raise _malformed("historical_discharge_mapping_invalid")
            observed = [
                row
                for row in obs.settlements
                if (row.kind, row.id)
                == (mapping.settlement_kind, mapping.settlement_id)
            ]
            if len(observed) != 1:
                raise _malformed("historical_discharge_mapping_invalid")
            settlement = observed[0]
            if (
                not settlement.active
                or settlement.invoice_id != invoice_id
                or settlement.id not in invoice.settlement_ids
                or settlement.source_id != mapping.source_id
                or effect.kind != settlement.kind
                or effect.kind not in {"payment", "credit_note"}
                or effect.amount_minor
                != _minor_units(settlement.amount, precisions.get(invoice.currency))
            ):
                raise _malformed("historical_discharge_mapping_invalid")
            used_references.add(effect.reference)
        histories[invoice_id] = effects
    adapter_blockers: list[tuple[str, str, str | None]] = []
    unmapped_accounts: set[str] = set()
    unmapped_invoices: set[str] = set()
    for invoice in obs.invoices:
        if (invoice.account_id, invoice.currency) not in partition_by_source:
            unmapped_accounts.add(str(invoice.account_id))
            unmapped_invoices.add(str(invoice.invoice_id))
    for control in obs.controls:
        if control.account_id is not None and control.currency is not None:
            if (control.account_id, control.currency) not in partition_by_source:
                unmapped_accounts.add(str(control.account_id))
    for account_id in sorted(unmapped_accounts):
        adapter_blockers.append(("unmapped_source_account", account_id, None))

    dispositions: list[OpeningDispositionV1] = []
    receivables: list[OpeningReceivableItemV1] = []
    used_mappings: list[OpeningRehearsalInvoiceMappingV1] = []
    receivable_invoice_ids: set[UUID] = set()
    try:
        for invoice in obs.invoices:
            # Precedence: blocking > excluded > eligible > blocking(unclassified).
            if invoice.invoice_id in blocking_codes:
                disposition = "blocking"
                reason = sorted(blocking_codes[invoice.invoice_id])[0]
            elif invoice.invoice_id in exclusion_reasons:
                disposition = "excluded"
                reason = exclusion_reasons[invoice.invoice_id]
            elif invoice.invoice_id in eligible_ids:
                disposition = "eligible"
                reason = "eligible"
            else:
                disposition = "blocking"
                reason = "unclassified"
            partition = partition_by_source.get((invoice.account_id, invoice.currency))
            residual_minor = _minor_units(
                invoice.residual, precisions.get(invoice.currency)
            )
            if residual_minor is None:
                adapter_blockers.append(
                    (
                        "currency_precision_mismatch",
                        str(invoice.invoice_id),
                        partition.source_account_key if partition else None,
                    )
                )
                disposition, reason = "blocking", "currency_precision_mismatch"
                residual_minor = 0  # blocked placeholder; never a rounded value
            if partition is None:
                continue
            mapping = invoice_mappings.get(invoice.invoice_id)
            if disposition == "eligible" and mapping is not None:
                identity = mapping.source_identity
            else:
                identity = OpeningSourceIdentityV1("invoice", str(invoice.invoice_id))
            dispositions.append(
                OpeningDispositionV1(
                    identity, partition, disposition, reason, residual_minor
                )
            )
            if disposition != "eligible":
                continue
            if mapping is None:
                unmapped_invoices.add(str(invoice.invoice_id))
                adapter_blockers.append(
                    (
                        "unmapped_source_invoice",
                        str(invoice.invoice_id),
                        partition.source_account_key,
                    )
                )
                continue
            # A deterministic pointer to the source row, labelled as a
            # pointer; it is not documentary evidence and qualifies nothing.
            pointer = f"source-row-pointer:invoice:{invoice.invoice_id}"
            receivables.append(
                OpeningReceivableItemV1(
                    mapping.source_identity,
                    partition,
                    mapping.source_invoice_key,
                    originals.get(invoice.invoice_id),
                    histories.get(invoice.invoice_id, ()),
                    residual_minor,
                    _collection_evidence(assembly, pointer),
                    assembly.receivable_mapping_version,
                    assembly.tax_mapping_version,
                    assembly.source_resolver_version,
                    pointer,
                )
            )
            used_mappings.append(mapping)
            receivable_invoice_ids.add(invoice.invoice_id)
    except OpeningEvidenceError:
        raise _malformed() from None
    unmapped_settlements: set[str] = set()
    discharge_keys = {
        (row.settlement_kind, row.settlement_id) for row in assembly.discharge_mappings
    }
    for settlement in obs.settlements:
        if settlement.active and settlement.invoice_id in eligible_ids:
            if (settlement.kind, settlement.id) not in discharge_keys:
                unmapped_settlements.add(str(settlement.id))
    for settlement_id in sorted(unmapped_settlements):
        adapter_blockers.append(("unmapped_source_settlement", settlement_id, None))

    distinct_partitions = tuple(
        dict.fromkeys(mapping.partition for mapping in assembly.partitions)
    )
    source_account_by_partition = {
        mapping.partition: mapping.source_account_id for mapping in assembly.partitions
    }
    lanes: dict[tuple[UUID, str], OpeningObservedLaneV1] = {}
    for lane in obs.lanes:
        lanes.setdefault((lane.account_id, lane.currency), lane)
    lane_controls: list[OpeningLaneControlsV1] = []
    for partition in distinct_partitions:
        lane = lanes.get((source_account_by_partition[partition], partition.currency))
        if lane is None:
            continue
        amounts = (
            lane.collectible_receivable,
            lane.unapplied_customer_credit,
            lane.prepaid_funding_reserved,
            lane.prepaid_funding_consumed,
            lane.written_off_total,
            lane.refunded_total,
            lane.adjustment_total,
        )
        minors = tuple(
            _minor_units(amount, precisions.get(partition.currency))
            for amount in amounts
        )
        if any(value is None for value in minors):
            adapter_blockers.append(
                (
                    "currency_precision_mismatch",
                    partition.source_account_key,
                    partition.source_account_key,
                )
            )
            continue
        exact = tuple(0 if value is None else value for value in minors)
        lane_controls.append(
            OpeningLaneControlsV1(
                partition,
                exact[0],
                0,  # available cash credit is the sum of lot residuals; no lots
                0,  # no non-cash credit is representable from today's evidence
                exact[1],
                exact[2],
                exact[3],
                exact[4],
                exact[5],
                exact[6],
                exact[0],
            )
        )
    dispositions.sort(
        key=lambda row: (
            row.partition.source_account_key,
            row.identity.kind,
            row.identity.key,
        )
    )
    receivables.sort(key=lambda row: row.source_invoice_key)
    lane_controls.sort(key=lambda row: row.partition.source_account_key)
    counts = OpeningPopulationCountsV1(
        len(dispositions),
        sum(1 for row in dispositions if row.disposition == "eligible"),
        sum(1 for row in dispositions if row.disposition == "excluded"),
        sum(1 for row in dispositions if row.disposition == "blocking"),
    )
    try:
        capture = OpeningCaptureV1(
            assembly.source_system,
            obs.source_repository_sha,
            str(obs.installation_id),
            obs.schema_heads,
            obs.snapshot_id,
            obs.captured_at,
            obs.economic_cutoff,
            assembly.recorded_cutoff,
            obs.isolation.replace(" ", "_"),
            obs.population_version,
            assembly.source_authority_ref,
            assembly.account_mapping_version,
            assembly.cash_mapping_version,
        )
        manifest = OpeningManifestV2(
            assembly.scope,
            capture,
            tuple(row.identity for row in dispositions),
            tuple(dispositions),
            counts,
            tuple(lane_controls),
            (),
            tuple(receivables),
        )
    except OpeningEvidenceError:
        raise _malformed("malformed_assembly") from None
    commands: list[ImportOpeningAccountV2] = []
    previews = []
    raw_blockers: list[tuple[OpeningPartitionV1, tuple[OpeningBlockerV1, ...]]] = []
    for partition in distinct_partitions:
        target = targets.get(partition)
        idempotency_key = idempotency_keys.get(partition)
        if target is None or idempotency_key is None:
            adapter_blockers.append(
                (
                    "target_unavailable",
                    partition.source_account_key,
                    partition.source_account_key,
                )
            )
            continue
        try:
            command = ImportOpeningAccountV2(
                assembly.scope,
                manifest,
                partition,
                target,
                assembly.migration_authority_ref,
                idempotency_key,
                assembly.occurred_at,
            )
        except OpeningEvidenceError:
            raise _malformed("malformed_assembly") from None
        commands.append(command)
        raw_blockers.append((partition, opening_account_import_blockers_v2(command)))
        previews.append(preview_opening_account_import_v2(command))
    settlements_by_key = {(row.kind, row.id): row for row in obs.settlements}
    used_discharge_mappings = tuple(
        mapping
        for mapping in assembly.discharge_mappings
        if (
            settlement := settlements_by_key.get(
                (mapping.settlement_kind, mapping.settlement_id)
            )
        )
        is not None
        and settlement.active
        and settlement.invoice_id in receivable_invoice_ids
    )
    run = OpeningRehearsalRunV1(
        source=obs,
        manifest=manifest,
        mappings=assembly.partitions,
        commands=tuple(commands),
        previews=tuple(previews),
        binding=assembly.binding,
        accepted_manual_holds=assembly.accepted_manual_holds,
        invoice_mappings=tuple(used_mappings),
        discharge_mappings=used_discharge_mappings,
    )
    unmapped = {
        "accounts": tuple(sorted(unmapped_accounts)),
        "invoices": tuple(sorted(unmapped_invoices)),
        "settlements": tuple(sorted(unmapped_settlements)),
    }
    return _AssemblyOutcome(run, tuple(adapter_blockers), unmapped, tuple(raw_blockers))


def assemble_rehearsal_run(
    obs: OpeningSubObservationV2,
    *,
    assembly: AssemblyInputs,
    sidecar: V3Sidecar | None = None,
    original_documents: tuple[tuple[UUID, OpeningOriginalDocumentV1], ...] = (),
    historical_discharges: tuple[
        tuple[UUID, tuple[OpeningHistoricalDischargeV1, ...]], ...
    ] = (),
) -> OpeningRehearsalRunV1:
    """Build a candidate run, preserving only explicitly supplied typed originals.

    This mapping is not qualification. The caller must independently qualify
    the exact command through the existing importer verifier. The source CLI
    supplies no originals unless explicitly given an aligned sidecar. Cash and
    discharge synthesis remain unsupported.

    The optional sidecar carries the exclusion/blocking identities the closed
    V2 projection cannot hold; without it those invoices safely degrade to
    blocking (unclassified) dispositions and remain counted blockers.
    """
    return _assemble(
        obs, sidecar or V3Sidecar(), assembly, original_documents, historical_discharges
    ).run


def _dropped_source_fields(payload: Mapping[str, object]) -> tuple[str, ...]:
    dropped: set[str] = set()
    root = _mapping(payload)
    capture = root.get("capture")
    if isinstance(capture, Mapping) and "mutable_history_reconstructed" in capture:
        dropped.add("capture.mutable_history_reconstructed")
    for item in _sequence(root.get("invoices", [])):
        if isinstance(item, Mapping):
            dropped.update(
                f"invoices.{key}"
                for key in item
                if key not in _PROJECTED_INVOICE_FIELDS
            )
    for item in _sequence(root.get("settlements", [])):
        if isinstance(item, Mapping):
            dropped.update(
                f"settlements.{key}"
                for key in item
                if key not in _PROJECTED_SETTLEMENT_FIELDS
            )
    for section in ("lines", "financial_sources", "effects", "subscriber_ids"):
        value = root.get(section)
        if isinstance(value, Sequence) and not isinstance(value, str) and len(value):
            dropped.add(section)
    cash = root.get("cash_evidence")
    if isinstance(cash, Mapping):
        for row_field in set(_CASH_ROW_FIELDS.values()):
            for item in _sequence(cash.get(row_field, [])):
                if isinstance(item, Mapping):
                    dropped.update(
                        f"cash_evidence.{row_field}.{key}"
                        for key in item
                        if key != "id"
                    )
    documentary = root.get("documentary_observations")
    if isinstance(documentary, Mapping):
        for section in ("current_invoices", "current_lines"):
            value = documentary.get(section)
            if (
                isinstance(value, Sequence)
                and not isinstance(value, str)
                and len(value)
            ):
                dropped.add(f"documentary_observations.{section}")
    return tuple(sorted(dropped))


def _grouped_blockers(
    rows: Sequence[tuple[str, Sequence[str]]],
) -> tuple[OpeningRehearsalCensusBlockerV1, ...]:
    grouped: dict[str, set[str]] = {}
    for code, references in rows:
        grouped.setdefault(code, set()).update(references)
    return tuple(
        OpeningRehearsalCensusBlockerV1(code, len(refs), tuple(sorted(refs)))
        for code, refs in sorted(grouped.items())
    )


def _counts(
    values: Mapping[str, int],
) -> tuple[OpeningRehearsalCensusCountV1, ...]:
    return tuple(
        OpeningRehearsalCensusCountV1(key, count)
        for key, count in sorted(values.items())
    )


def rehearsal_census(
    payload: Mapping[str, object],
    *,
    assembly: AssemblyInputs,
    original_documents: tuple[tuple[UUID, OpeningOriginalDocumentV1], ...] = (),
    historical_discharges: tuple[
        tuple[UUID, tuple[OpeningHistoricalDischargeV1, ...]], ...
    ] = (),
) -> OpeningRehearsalCensusV1:
    """Run the census, preserving independently supplied typed originals.

    Originals use the same mapping checks as ``assemble_rehearsal_run``;
    supplying them does not qualify the preview or discharge report blockers.
    V3 historical-unavailable observations remain visible in the artifact.
    """
    obs, sidecar = load_sub_observation(payload)
    outcome = _assemble(
        obs, sidecar, assembly, original_documents, historical_discharges
    )
    run = outcome.run
    report = report_opening_rehearsal(run)
    raw_by_partition = dict(outcome.raw_blockers)
    previews = {preview.partition: preview for preview in run.previews}
    source_account_by_partition = {
        mapping.partition: mapping.source_account_id for mapping in run.mappings
    }
    scoped_blockers: dict[str, list[tuple[str, Sequence[str]]]] = {}
    global_blockers: list[tuple[str, Sequence[str]]] = []
    for code, reference, partition_key in outcome.adapter_blockers:
        if partition_key is None:
            global_blockers.append((code, (reference,)))
        else:
            scoped_blockers.setdefault(partition_key, []).append((code, (reference,)))
    partitions: list[OpeningRehearsalCensusPartitionV1] = []
    for partition in sorted(
        {mapping.partition for mapping in run.mappings},
        key=lambda row: row.source_account_key,
    ):
        preview = previews.get(partition)
        rows: list[tuple[str, Sequence[str]]] = []
        if preview is not None:
            rows.extend((row.code, row.references) for row in preview.blockers)
        rows.extend(
            (row.code, row.references) for row in raw_by_partition.get(partition, ())
        )
        rows.extend(scoped_blockers.get(partition.source_account_key, []))
        blockers = _grouped_blockers(rows)
        partitions.append(
            OpeningRehearsalCensusPartitionV1(
                str(source_account_by_partition[partition]),
                partition.source_account_key,
                partition.currency,
                partition.precision,
                bool(preview and preview.admissible) and not blockers,
                preview.cash_residual_minor if preview else 0,
                preview.receivable_residual_minor if preview else 0,
                blockers,
                tuple(
                    OpeningRehearsalCensusDeltaV1(
                        delta.lane,
                        delta.source_minor,
                        delta.preview_minor,
                        delta.unclassified_delta_minor,
                    )
                    for delta in report.deltas
                    if delta.partition == partition
                ),
            )
        )
    by_requirement: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for record in sidecar.unavailable_original_evidence:
        for requirement in record.requirements:
            by_requirement[requirement] = by_requirement.get(requirement, 0) + 1
        for reason in record.reasons:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    blocked_by_reason: dict[str, int] = {}
    for entry in partitions:
        for blocker in entry.blockers:
            blocked_by_reason[blocker.code] = blocked_by_reason.get(blocker.code, 0) + 1
    report_blockers = _grouped_blockers(
        [(row.code, row.references) for row in report.blockers]
    )
    unmapped = OpeningRehearsalCensusUnmappedV1(
        outcome.unmapped["accounts"],
        outcome.unmapped["invoices"],
        outcome.unmapped["settlements"],
    )
    return OpeningRehearsalCensusV1(
        schema=_ARTIFACT_SCHEMA,
        provenance=OpeningRehearsalCensusProvenanceV1(
            obs.source_repository_sha,
            assembly.starter_head_sha,
            obs.population_version,
            obs.content_fingerprint,
            obs.snapshot_id,
            obs.schema_heads,
            obs.captured_at.isoformat(),
            assembly.sub_exit_code,
            assembly.assembly_digest,
            report.binding_digest,
            report.content_digest,
            opening_manifest_fingerprint_v2(run.manifest),
        ),
        partitions=tuple(partitions),
        report_blockers=report_blockers,
        adapter_blockers=_grouped_blockers(global_blockers),
        unmapped=unmapped,
        v3_documentary_sidecar=OpeningRehearsalCensusSidecarV1(
            len(sidecar.unavailable_original_evidence),
            _counts(by_requirement),
            _counts(by_reason),
            tuple(
                sorted(
                    str(record.invoice_id)
                    for record in sidecar.unavailable_original_evidence
                )
            ),
        ),
        dropped_source_fields=_dropped_source_fields(payload),
        rollup=OpeningRehearsalCensusRollupV1(
            len(partitions),
            sum(1 for entry in partitions if entry.admissible),
            bool(
                report.complete_exact_reconciliation
                and not outcome.adapter_blockers
                and not any(outcome.unmapped.values())
            ),
            _counts(blocked_by_reason),
            assembly.accepted_manual_holds,
        ),
    )


def load_original_evidence(
    payload: object, *, census_bytes: bytes, assembly: AssemblyInputs
) -> OpeningRehearsalOriginalEvidenceV1:
    """Require exact raw census and canonical assembly alignment, not authority."""
    try:
        evidence = parse_opening_rehearsal_original_evidence(payload)
    except OpeningEvidenceError:
        raise _malformed("malformed_original_evidence") from None
    if (
        evidence.census_sha256 != hashlib.sha256(census_bytes).hexdigest()
        or evidence.assembly_digest != assembly.assembly_digest
    ):
        raise _malformed("malformed_original_evidence")
    return evidence


def _read_census(path: Path) -> tuple[bytes, Mapping[str, object]]:
    try:
        raw = path.read_bytes()
        return raw, _mapping(parse_opening_rehearsal_json(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, OpeningEvidenceError):
        raise _malformed() from None


def _read_json(path: Path, code: str = "malformed_input") -> Mapping[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise _malformed(code) from None
    try:
        value = parse_opening_rehearsal_json(text)
    except OpeningEvidenceError:
        raise _malformed(code) from None
    return _mapping(value, code)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 zero blockers, 1 census complete with blockers, 2 malformed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", required=True, help="Sub v2/v3 census JSON")
    parser.add_argument(
        "--assembly",
        required=True,
        help="Assembly declaration JSON; the adapter refuses to run without it",
    )
    parser.add_argument(
        "--original-evidence",
        help="Optional V1 original evidence aligned to census bytes and assembly",
    )
    parser.add_argument("--out", help="Write the artifact here instead of stdout")
    args = parser.parse_args(argv)
    try:
        census_bytes, census_payload = _read_census(Path(args.census))
        assembly = load_assembly(_read_json(Path(args.assembly), "malformed_assembly"))
        originals = ()
        histories = ()
        if args.original_evidence:
            evidence = load_original_evidence(
                _read_json(Path(args.original_evidence), "malformed_original_evidence"),
                census_bytes=census_bytes,
                assembly=assembly,
            )
            originals = tuple(
                (row.source_invoice_id, row.original) for row in evidence.invoices
            )
            histories = tuple(
                (row.source_invoice_id, row.historical_discharges)
                for row in evidence.invoices
            )
        artifact = rehearsal_census(
            census_payload,
            assembly=assembly,
            original_documents=originals,
            historical_discharges=histories,
        )
    except AdapterInputError as exc:
        print(render_opening_rehearsal_refusal(exc.code))
        return 2
    except OpeningEvidenceError:
        print(render_opening_rehearsal_refusal("malformed_input"))
        return 2
    rendered = render_opening_rehearsal_census(artifact)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 1 if artifact.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
