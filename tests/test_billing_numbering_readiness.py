"""Readiness canaries: pure proof, never allocation."""

from datetime import date
from uuid import UUID

import pytest

from app.services.billing_numbering_readiness import (
    FindingCode,
    LegacySeriesConfiguration,
    ModuleCounter,
    ModuleReceiptObservation,
    ModuleSeriesConfiguration,
    NumberingReadinessQuery,
    NumberObservation,
    SeedPlan,
    SeriesKind,
    build_seed_plan,
)

QUERY = NumberingReadinessQuery(reference_date=date(2026, 10, 13))


def observation(value: str | None, ordinal: int = 1) -> NumberObservation:
    return NumberObservation(document_id=UUID(int=ordinal), raw_number=value)


def plan(
    values: tuple[str | None, ...],
    *,
    padding: int = 6,
    prefix: str = "INV-",
    legacy_next: int | None = None,
    total: int | None = None,
) -> SeedPlan:
    return build_seed_plan(
        query=QUERY,
        kind=SeriesKind.INVOICE,
        configuration=LegacySeriesConfiguration(
            prefix=prefix, padding=padding, start_value=1
        ),
        observations=tuple(
            observation(value, index + 1) for index, value in enumerate(values)
        ),
        database_total=len(values) if total is None else total,
        legacy_next_value=legacy_next,
    )


def test_census_includes_maximum_and_never_rewinds() -> None:
    result = plan(("INV-000900", "INV-000001"), legacy_next=1200)
    assert result.admissible and result.proposed_next_value == 1200
    assert result.preview_number == "INV-001200"
    assert plan(("INV-000900",), legacy_next=2).proposed_next_value == 901


@pytest.mark.parametrize("padding", (0, 1, 6, 18))
def test_public_formatter_keeps_positive_legacy_bytes(padding: int) -> None:
    result = plan((f"λ/{7:0{padding}d}",), padding=padding, prefix="λ/")
    assert result.admissible and result.maximum_matching_value == 7
    assert result.preview_number == f"λ/{8:0{padding}d}"


def test_exact_ascii_positive_round_trip_grammar_and_population() -> None:
    result = plan(
        (None, "", "INV-000001", "INV-000000", "INV-00001", "INV-٠٠٠٠٠١", " inv-000001")
    )
    assert (
        result.counts.null,
        result.counts.empty,
        result.counts.matching,
        result.counts.other,
    ) == (1, 1, 1, 4)
    assert result.counts.scanned == result.counts.database_total == 7


def test_duplicate_history_is_visible_but_cannot_collide_with_next() -> None:
    result = plan(("INV-000001", "INV-000001"))
    assert result.admissible and result.proposed_next_value == 2
    assert FindingCode.DUPLICATE_NUMBER in {finding.code for finding in result.findings}


def test_incomplete_population_cannot_admit() -> None:
    result = plan(("INV-000001",), total=2)
    assert not result.complete and not result.admissible
    assert result.proposed_next_value is None


@pytest.mark.parametrize("prefix,padding", (("x" * 33, 6), ("INV-", 19), ("INV-", -1)))
def test_unsupported_configuration_refuses_plan(prefix: str, padding: int) -> None:
    assert not plan((), prefix=prefix, padding=padding).admissible


def test_exhaustion_and_invalid_counter_refuse_plan() -> None:
    assert not plan(("INV-999999999999999",)).admissible
    assert not plan((), legacy_next=0).admissible


def test_digest_binds_identity_config_counter_and_all_raw_numbers() -> None:
    assert (
        plan(("INV-000001",)).evidence_digest == plan(("INV-000001",)).evidence_digest
    )
    baseline = plan(("INV-000001",)).evidence_digest
    assert baseline != plan(("unrecognised",)).evidence_digest
    assert baseline != plan(("INV-000001",), legacy_next=9).evidence_digest
    assert baseline != plan(("INV-000001",), prefix="OTHER-").evidence_digest


def test_module_counter_ahead_is_preserved_and_configuration_is_bound() -> None:
    configuration = LegacySeriesConfiguration(prefix="INV-", padding=6, start_value=1)
    module = ModuleSeriesConfiguration(
        prefix="INV-",
        suffix="",
        separator="",
        min_digits=6,
        include_year=0,
        year_digits=4,
        include_month=0,
        reset_policy="never",
        start_value=1,
    )
    result = build_seed_plan(
        query=QUERY,
        kind=SeriesKind.INVOICE,
        configuration=configuration,
        observations=(),
        database_total=0,
        module_configuration=module,
        module_counters=(ModuleCounter(period_key="*", next_value=900),),
    )
    assert result.admissible and result.proposed_next_value == 900
    orphan = build_seed_plan(
        query=QUERY,
        kind=SeriesKind.INVOICE,
        configuration=configuration,
        observations=(),
        database_total=0,
        module_counters=(ModuleCounter(period_key="*", next_value=900),),
    )
    assert not orphan.admissible
    wrong = ModuleSeriesConfiguration(
        prefix="INV-",
        suffix="",
        separator="-",
        min_digits=6,
        include_year=0,
        year_digits=4,
        include_month=0,
        reset_policy="never",
        start_value=1,
    )
    assert not build_seed_plan(
        query=QUERY,
        kind=SeriesKind.INVOICE,
        configuration=configuration,
        observations=(),
        database_total=0,
        module_configuration=wrong,
    ).admissible
    assert not build_seed_plan(
        query=QUERY,
        kind=SeriesKind.INVOICE,
        configuration=configuration,
        observations=(),
        database_total=0,
        module_configuration=module,
        module_counters=(ModuleCounter(period_key="2026", next_value=900),),
    ).admissible


def test_order_and_document_identity_are_part_of_complete_proof() -> None:
    configuration = LegacySeriesConfiguration(prefix="", padding=0, start_value=7)
    first = observation("8", 1)
    second = observation("9", 2)
    complete = build_seed_plan(
        query=QUERY,
        kind=SeriesKind.CREDIT_NOTE,
        configuration=configuration,
        observations=(first, second),
        database_total=2,
    )
    unordered = build_seed_plan(
        query=QUERY,
        kind=SeriesKind.CREDIT_NOTE,
        configuration=configuration,
        observations=(second, first),
        database_total=2,
    )
    assert complete.admissible and complete.proposed_next_value == 10
    assert not unordered.complete and not unordered.admissible
    moved = build_seed_plan(
        query=QUERY,
        kind=SeriesKind.CREDIT_NOTE,
        configuration=configuration,
        observations=(observation("8", 3), observation("9", 4)),
        database_total=2,
    )
    assert moved.evidence_digest != complete.evidence_digest


def test_cli_cache_is_disabled_for_query_and_restored_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from dotmac_kernel.cache import MemoryCache
    from dotmac_kernel.settings_cache import (
        active_settings_cache,
        install_settings_cache,
    )
    from sqlalchemy.orm import Session

    from app.services.billing_numbering_readiness import NumberingReadinessError
    from scripts.migration import billing_numbering_readiness as cli

    @contextmanager
    def snapshot() -> Iterator[Session]:
        assert active_settings_cache() is None
        yield Session()

    def unavailable(db: Session, *, query: NumberingReadinessQuery) -> None:
        assert active_settings_cache() is None
        assert query == QUERY
        raise NumberingReadinessError("incomplete_population")

    previous = active_settings_cache()
    stale = MemoryCache()
    stale.set("deliberately-stale", "not-an-authoritative-setting")
    install_settings_cache(stale)
    monkeypatch.setattr(cli, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(cli, "billing_numbering_readiness_report", unavailable)
    try:
        assert cli.main(["--reference-date", QUERY.reference_date.isoformat()]) == 2
        assert active_settings_cache() is stale
        assert stale.get("deliberately-stale") == "not-an-authoritative-setting"
    finally:
        install_settings_cache(previous)
    assert "incomplete_population" in capsys.readouterr().out


def test_module_receipts_require_complete_consistent_counter_and_bind_all_fields() -> (
    None
):
    from dataclasses import replace

    configuration = LegacySeriesConfiguration(prefix="INV-", padding=6, start_value=1)
    module = ModuleSeriesConfiguration(
        prefix="INV-",
        suffix="",
        separator="",
        min_digits=6,
        include_year=0,
        year_digits=4,
        include_month=0,
        reset_policy="never",
        start_value=1,
    )
    receipt = ModuleReceiptObservation(
        receipt_id=UUID(int=1),
        period_key="*",
        allocated_value=8,
        formatted_number="INV-000008",
        reference_date=QUERY.reference_date,
    )

    def receipt_plan(
        receipts: tuple[ModuleReceiptObservation, ...],
        counters: tuple[ModuleCounter, ...],
        total: int = 1,
    ) -> SeedPlan:
        return build_seed_plan(
            query=QUERY,
            kind=SeriesKind.INVOICE,
            configuration=configuration,
            observations=(),
            database_total=0,
            module_configuration=module,
            module_counters=counters,
            module_receipts=receipts,
            module_receipt_total=total,
        )

    counter = (ModuleCounter(period_key="*", next_value=9),)
    result = receipt_plan((receipt,), counter)
    assert result.admissible and result.proposed_next_value == 9
    assert result.module_receipts.maximum_by_period[0].allocated_value == 8
    assert not receipt_plan((receipt,), ()).admissible
    assert not receipt_plan(
        (receipt,), (ModuleCounter(period_key="*", next_value=8),)
    ).admissible
    assert not receipt_plan((receipt,), counter, total=2).complete
    for changed in (
        replace(receipt, allocated_value=7),
        replace(receipt, formatted_number="different"),
        replace(receipt, reference_date=date(2026, 10, 14)),
        replace(receipt, receipt_id=UUID(int=2)),
    ):
        assert (
            receipt_plan((changed,), counter).evidence_digest != result.evidence_digest
        )
    assert not receipt_plan(
        (replace(receipt, formatted_number="different"),), counter
    ).admissible
    assert not receipt_plan((replace(receipt, period_key="2026"),), counter).admissible
