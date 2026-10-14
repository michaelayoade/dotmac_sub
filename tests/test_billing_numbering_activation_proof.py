"""Pure candidate proofs; synthetic grammars never select production policy."""

from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest
from dotmac_numbering import SeriesConfiguration

from app.services.billing_numbering_activation_proof import (
    ActivationProofQuery,
    ActivationProofReport,
    build_activation_proof,
    disjoint_head_witness,
)
from app.services.billing_numbering_readiness import (
    LegacySeriesConfiguration,
    NumberObservation,
)

DATE = date(2026, 10, 14)
TARGET = SeriesConfiguration(
    series_code="invoice_number",
    prefix="NEW",
    suffix="END",
    separator="-",
    min_digits=3,
    include_year=False,
    year_digits=4,
    include_month=False,
    reset_policy="never",
    start_value=1,
)
LEGACY = LegacySeriesConfiguration(prefix="INV-", padding=3, start_value=1)


@pytest.mark.parametrize(
    ("legacy", "head", "expected"),
    [
        ("INV-", "NEW-", True),
        ("INV-", "INV-X-", True),
        ("INV-", "INV-1", False),
        ("INV-", "INV", False),
        ("", "123", False),
        ("", "λ", True),
        ("123", "1234", False),
        ("A", "AB", True),
        ("AB", "A", False),
    ],
)
def test_conservative_language_witness(legacy: str, head: str, expected: bool) -> None:
    assert disjoint_head_witness(legacy, head) is expected


def proof(
    labels: tuple[str | None, ...], target: SeriesConfiguration = TARGET
) -> ActivationProofReport:
    return build_activation_proof(
        query=ActivationProofQuery(reference_date=DATE, proposal=target),
        legacy_configuration=LEGACY,
        observations=tuple(
            NumberObservation(document_id=UUID(int=i + 1), raw_number=label)
            for i, label in enumerate(labels)
        ),
        database_total=len(labels),
    )


@pytest.mark.parametrize("label", ["NEW-001-END", "NEW-1000-END"])
def test_other_legacy_label_matching_target_blocks_first_activation(label: str) -> None:
    result = proof((label,))
    assert result.target_matches == 1
    assert "historical_target_collision" in result.blockers
    assert not result.candidate_proven


@pytest.mark.parametrize(
    "label",
    [
        "NEW-01-END",
        "NEW-0001-END",
        "NEW-000-END",
        "NEW-١٢٣-END",
        "NEW-001-ENDx",
        "NEW-+01-END",
    ],
)
def test_exact_public_format_near_misses(label: str) -> None:
    assert proof((label,)).target_matches == 0


def test_full_population_null_empty_other_and_digest() -> None:
    result = proof((None, "", "INV-001", "foreign", "foreign"))
    assert (
        result.scanned,
        result.null,
        result.empty,
        result.legacy_matches,
        result.other,
        result.duplicate_occurrences,
    ) == (5, 1, 1, 1, 2, 1)
    assert result.complete and result.candidate_proven
    assert not result.activatable
    assert result.actual_preview is None
    assert "missing_target_configuration" in result.blockers
    assert (
        result.evidence_digest
        != proof((None, "", "INV-001", "changed", "foreign")).evidence_digest
    )


def test_incomplete_and_unordered_population_fail_closed() -> None:
    query = ActivationProofQuery(reference_date=DATE, proposal=TARGET)
    rows = (
        NumberObservation(UUID(int=2), "INV-001"),
        NumberObservation(UUID(int=1), None),
    )
    result = build_activation_proof(
        query=query, legacy_configuration=LEGACY, observations=rows, database_total=3
    )
    assert not result.complete and not result.candidate_proven


@pytest.mark.parametrize(
    "target",
    [
        replace(TARGET, include_year=True),
        replace(TARGET, include_month=True),
        replace(TARGET, reset_policy="yearly"),
        replace(TARGET, series_code="credit_note_number"),
    ],
)
def test_unsupported_policy_is_explicit(target: SeriesConfiguration) -> None:
    assert "unsupported_proposal" in proof((), target).blockers


def test_actual_preview_requires_exact_config_and_sufficient_seed() -> None:
    from datetime import UTC, datetime

    from app.services.billing_numbering_activation_proof import (
        TargetCounterObservation,
        TargetSeriesObservation,
    )
    from app.services.billing_numbering_readiness import SCOPE

    now = datetime(2026, 10, 14, tzinfo=UTC)
    series = TargetSeriesObservation(UUID(int=1), SCOPE.tenant_id, TARGET, now, now)
    counter = TargetCounterObservation(
        UUID(int=2), SCOPE.tenant_id, TARGET.series_code, "*", 1, now, now
    )

    def observed(value: int, rendered: str) -> ActivationProofReport:
        return build_activation_proof(
            query=ActivationProofQuery(DATE, TARGET),
            legacy_configuration=LEGACY,
            observations=(NumberObservation(UUID(int=3), "INV-900"),),
            database_total=1,
            target_series=series,
            counters=(replace(counter, next_value=value),),
            actual_preview=rendered,
        )

    low = observed(1, "NEW-001-END")
    assert not low.actual_preview_verified
    assert "insufficient_seeded_counter" in low.blockers
    ahead = observed(902, "NEW-902-END")
    assert ahead.actual_preview_verified and not ahead.activatable
    assert ahead.evidence_digest != observed(903, "NEW-903-END").evidence_digest
    mismatch = build_activation_proof(
        query=ActivationProofQuery(DATE, TARGET),
        legacy_configuration=LEGACY,
        observations=(),
        database_total=0,
        target_series=replace(series, configuration=replace(TARGET, suffix="wrong")),
        counters=(counter,),
        actual_preview="NEW-001-END",
    )
    assert (
        not mismatch.actual_preview_verified
        and "target_configuration_mismatch" in mismatch.blockers
    )


def test_receipt_fields_bound_without_claiming_document_provenance() -> None:
    from datetime import UTC, datetime

    from app.services.billing_numbering_activation_proof import TargetReceiptObservation
    from app.services.billing_numbering_readiness import SCOPE

    receipt = TargetReceiptObservation(
        UUID(int=1),
        SCOPE.tenant_id,
        TARGET.series_code,
        "*",
        1,
        "NEW-001-END",
        DATE,
        "unlinked-key",
        datetime(2026, 10, 14, tzinfo=UTC),
        None,
    )

    def observed(row: TargetReceiptObservation) -> ActivationProofReport:
        return build_activation_proof(
            query=ActivationProofQuery(DATE, TARGET),
            legacy_configuration=LEGACY,
            observations=(),
            database_total=0,
            receipts=(row,),
            receipt_total=1,
        )

    result = observed(receipt)
    assert not result.candidate_proven
    assert "existing_target_history_requires_reconciliation" in result.blockers
    assert (
        result.evidence_digest
        != observed(replace(receipt, idempotency_key="different")).evidence_digest
    )
    assert (
        result.evidence_digest
        != observed(replace(receipt, allocated_by="observer")).evidence_digest
    )
    assert "unlinked-key" not in str(result.as_dict())


@pytest.mark.parametrize(
    "number", ["NEW-10000000000000000000-END", "NEW-999999999999999999999999999999-END"]
)
def test_long_historical_public_format_remains_collision(number: str) -> None:
    assert proof((number,)).target_matches == 1


@pytest.mark.parametrize(
    "target",
    [
        replace(TARGET, min_digits=True),
        replace(TARGET, start_value=True),
        replace(TARGET, start_value=0),
        replace(TARGET, start_value=10**15),
        replace(TARGET, min_digits=19),
        replace(TARGET, prefix="p" * 33),
        replace(TARGET, prefix="p" * 32, suffix="s" * 32, separator="-" * 8),
    ],
)
def test_unsafe_numeric_and_storage_proposals_refused(
    target: SeriesConfiguration,
) -> None:
    assert not proof((), target).candidate_proven


def test_legacy_large_exact_population_does_not_silently_become_other() -> None:
    result = proof(("INV-100000000000000000000",))
    assert result.legacy_matches == 1 and result.other == 0
    assert result.proposed_next_value is None
    assert "exhausted_proposed_counter" in result.blockers


def test_effective_legacy_start_floor_requires_sufficient_target_counter() -> None:
    from datetime import UTC, datetime

    from app.services.billing_numbering_activation_proof import (
        TargetCounterObservation,
        TargetSeriesObservation,
    )
    from app.services.billing_numbering_readiness import SCOPE

    now = datetime(2026, 10, 14, tzinfo=UTC)
    legacy = replace(LEGACY, start_value=1000)
    series = TargetSeriesObservation(UUID(int=1), SCOPE.tenant_id, TARGET, now, now)
    counter = TargetCounterObservation(
        UUID(int=2), SCOPE.tenant_id, TARGET.series_code, "*", 1, now, now
    )

    def observed(value: int, rendered: str) -> ActivationProofReport:
        return build_activation_proof(
            query=ActivationProofQuery(DATE, TARGET),
            legacy_configuration=legacy,
            observations=(),
            database_total=0,
            legacy_next_value=None,
            target_series=series,
            counters=(replace(counter, next_value=value),),
            actual_preview=rendered,
        )

    low = observed(1, "NEW-001-END")
    assert low.proposed_next_value == 1000
    assert not low.actual_preview_verified
    assert "insufficient_seeded_counter" in low.blockers
    sufficient = observed(1000, "NEW-1000-END")
    assert sufficient.proposed_next_value == 1000
    assert sufficient.actual_preview_verified
    assert "insufficient_seeded_counter" not in sufficient.blockers
    assert not sufficient.activatable


def test_public_preview_fallback_cannot_replace_missing_seeded_counter() -> None:
    from datetime import UTC, datetime

    from app.services.billing_numbering_activation_proof import TargetSeriesObservation
    from app.services.billing_numbering_readiness import SCOPE

    now = datetime(2026, 10, 14, tzinfo=UTC)
    series = TargetSeriesObservation(UUID(int=1), SCOPE.tenant_id, TARGET, now, now)
    result = build_activation_proof(
        query=ActivationProofQuery(DATE, TARGET),
        legacy_configuration=LEGACY,
        observations=(),
        database_total=0,
        target_series=series,
        counters=(),
        actual_preview="NEW-001-END",
    )
    assert not result.seeded_counter_present
    assert not result.actual_preview_verified
    assert "missing_or_invalid_seeded_counter" in result.blockers
    assert "actual_public_preview_unmet" in result.blockers
    assert not result.activatable
