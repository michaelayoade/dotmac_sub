from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing import DueDateBasisStatus, DueDateBasisV1

from sub_billing_adoption.classification import classify_legacy_fact
from sub_billing_adoption.contracts import (
    EvidenceState,
    FactLifecycle,
    LegacyDisposition,
    LegacyFactKind,
    LegacyFinancialFactV1,
    SourceAuthority,
)
from sub_billing_adoption.errors import AdoptionErrorCode, BillingAdoptionError


def _verified_due_date() -> DueDateBasisV1:
    instant = datetime(2026, 8, 1, tzinfo=UTC)
    return DueDateBasisV1(
        status=DueDateBasisStatus.VERIFIED,
        source_authority="subscriptions",
        evidence_ref="contract:1:v3",
        payment_terms_code="net-30",
        payment_terms_version="3",
        issued_at=instant,
        effective_at=instant,
        timezone="Africa/Lagos",
        derivation_policy="calendar-days",
        derivation_version="1",
    )


def _fact() -> LegacyFinancialFactV1:
    return LegacyFinancialFactV1(
        tenant_id=uuid4(),
        fact_kind=LegacyFactKind.INVOICE,
        fact_id=uuid4(),
        account_id=uuid4(),
        currency="NGN",
        minor_units=2,
        amount=Decimal("1250.50"),
        source_authority=SourceAuthority.NATIVE_INTERNAL,
        lifecycle=FactLifecycle.OPEN,
        source_ref="invoice:1",
        source_version="7",
        observed_at=datetime(2026, 8, 17, tzinfo=UTC),
        due_date_basis=_verified_due_date(),
        settlement_evidence=EvidenceState.NOT_APPLICABLE,
        allocation_evidence=EvidenceState.NOT_APPLICABLE,
        tax_evidence=EvidenceState.COMPLETE,
        fx_evidence=EvidenceState.NOT_APPLICABLE,
    )


def test_total_classifier_covers_all_five_dispositions() -> None:
    complete = _fact()
    provider = replace(
        complete,
        fact_id=uuid4(),
        source_authority=SourceAuthority.PROVIDER_OWNED,
        due_date_basis=DueDateBasisV1.unknown_unverified(
            source_authority="splynx", evidence_ref="invoice:provider:1"
        ),
    )
    closed_gap = replace(
        complete,
        fact_id=uuid4(),
        lifecycle=FactLifecycle.CLOSED,
        due_date_basis=None,
    )
    open_gap = replace(complete, fact_id=uuid4(), due_date_basis=None)
    incorrect = replace(complete, fact_id=uuid4(), known_incorrect=True)

    assert (
        classify_legacy_fact(complete).disposition is LegacyDisposition.TARGET_BACKFILL
    )
    assert (
        classify_legacy_fact(provider).disposition
        is LegacyDisposition.PROVIDER_PROJECTION
    )
    assert (
        classify_legacy_fact(closed_gap).disposition
        is LegacyDisposition.CLOSED_LEGACY_ARCHIVE
    )
    assert (
        classify_legacy_fact(open_gap).disposition is LegacyDisposition.CUTOVER_BLOCKER
    )
    assert (
        classify_legacy_fact(incorrect).disposition
        is LegacyDisposition.KNOWN_INCORRECT_NATIVE_FACT
    )


def test_unknown_due_date_is_reportable_but_never_collectible() -> None:
    fact = replace(
        _fact(),
        due_date_basis=DueDateBasisV1.unknown_unverified(
            source_authority="legacy-import", evidence_ref="invoice:1:null-basis"
        ),
    )

    result = classify_legacy_fact(fact)

    assert result.disposition is LegacyDisposition.CUTOVER_BLOCKER
    assert "verified_due_date_basis" in result.missing_evidence
    assert result.collectible is False
    assert result.accounting_reemit_allowed is False


def test_source_fingerprint_is_reproducible_and_sensitive() -> None:
    fact = _fact()
    first = classify_legacy_fact(fact)
    replay = classify_legacy_fact(fact)
    changed = classify_legacy_fact(replace(fact, amount=Decimal("1250.51")))

    assert first.evidence_fingerprint == replay.evidence_fingerprint
    assert first.evidence_fingerprint != changed.evidence_fingerprint


def test_float_input_fails_at_the_typed_boundary() -> None:
    with pytest.raises(BillingAdoptionError) as caught:
        replace(_fact(), amount=1.5)  # type: ignore[arg-type]

    assert caught.value.code is AdoptionErrorCode.INVALID_SOURCE_FACT
