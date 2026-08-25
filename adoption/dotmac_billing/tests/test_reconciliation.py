from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sub_billing_adoption.errors import AdoptionErrorCode, BillingAdoptionError
from sub_billing_adoption.reconciliation import (
    ComparisonKeyV1,
    CustomerImpact,
    DriftAcceptanceV1,
    DriftClassification,
    FinancialObservationV1,
    FinancialSurface,
    ObservationSource,
    reconcile_shadow,
)


def _key() -> ComparisonKeyV1:
    return ComparisonKeyV1(
        tenant_id=uuid4(),
        account_id=uuid4(),
        currency="NGN",
        surface=FinancialSurface.POSITION,
        source_identity="account:1:NGN",
    )


def _observations(
    key: ComparisonKeyV1, *digests: str
) -> tuple[FinancialObservationV1, ...]:
    return tuple(
        FinancialObservationV1(key=key, source=source, digest_sha256=digest)
        for source, digest in zip(ObservationSource, digests, strict=True)
    )


def test_exact_three_way_match_is_complete_and_reproducible() -> None:
    key = _key()
    digest = "a" * 64
    run_id = uuid4()
    instant = datetime(2026, 8, 17, tzinfo=UTC)

    first = reconcile_shadow(
        run_id=run_id,
        observed_at=instant,
        observations=_observations(key, digest, digest, digest),
    )
    replay = reconcile_shadow(
        run_id=run_id,
        observed_at=instant,
        observations=_observations(key, digest, digest, digest),
    )

    assert first.complete
    assert first.unclassified_count == 0
    assert first.results[0].classification is DriftClassification.MATCH
    assert first.report_fingerprint == replay.report_fingerprint


def test_one_smallest_digest_difference_has_no_tolerance() -> None:
    key = _key()
    report = reconcile_shadow(
        run_id=uuid4(),
        observed_at=datetime.now(UTC),
        observations=_observations(key, "a" * 64, "b" * 64, "a" * 64),
    )

    assert not report.complete
    assert report.unclassified_count == 1
    assert report.results[0].classification is DriftClassification.UNCLASSIFIED


def test_customer_impact_requires_explicit_review_reference() -> None:
    with pytest.raises(BillingAdoptionError) as caught:
        DriftAcceptanceV1(
            key=_key(),
            classification=DriftClassification.INTENTIONAL_CORRECTION,
            impact=CustomerImpact.CUSTOMER_DEBIT,
            review_reference=None,
            rationale="correct source defect",
        )

    assert caught.value.code is AdoptionErrorCode.INVALID_DRIFT_ACCEPTANCE


def test_reviewed_drift_is_classified_without_becoming_a_match() -> None:
    key = _key()
    acceptance = DriftAcceptanceV1(
        key=key,
        classification=DriftClassification.SOURCE_DEFECT,
        impact=CustomerImpact.NONE,
        review_reference=None,
        rationale="legacy position hash omitted one reversing group",
    )

    report = reconcile_shadow(
        run_id=uuid4(),
        observed_at=datetime.now(UTC),
        observations=_observations(key, "a" * 64, "b" * 64, "b" * 64),
        acceptances=(acceptance,),
    )

    assert report.complete
    assert report.results[0].classification is DriftClassification.SOURCE_DEFECT


def test_duplicate_source_observation_fails_closed() -> None:
    key = _key()
    row = FinancialObservationV1(
        key=key, source=ObservationSource.LEGACY, digest_sha256="a" * 64
    )
    with pytest.raises(BillingAdoptionError) as caught:
        reconcile_shadow(
            run_id=uuid4(),
            observed_at=datetime.now(UTC),
            observations=(row, row),
        )

    assert caught.value.code is AdoptionErrorCode.DUPLICATE_RECONCILIATION_OBSERVATION
