from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sub_billing_adoption.contracts import (
    AuthorityPhase,
    BillingAuthorityState,
    CoupledAuthorityWatermarkV1,
    DispositionResultV1,
    LegacyDisposition,
    LegacyFactKind,
    SourceHighWatermarkV1,
    WriterState,
)
from sub_billing_adoption.cutover import (
    CutoverEvidenceV1,
    ReadinessBlocker,
    RollbackDisposition,
    evaluate_cutover_readiness,
    rollback_disposition,
)
from sub_billing_adoption.errors import AdoptionErrorCode, BillingAdoptionError
from sub_billing_adoption.reconciliation import (
    ComparisonKeyV1,
    FinancialObservationV1,
    FinancialSurface,
    ObservationSource,
    ReconciliationReportV1,
    reconcile_shadow,
)

_DIGEST = "a" * 64


def _disposition(value: LegacyDisposition) -> DispositionResultV1:
    return DispositionResultV1(
        fact_id=uuid4(),
        fact_kind=LegacyFactKind.INVOICE,
        disposition=value,
        missing_evidence=(),
        collectible=value is LegacyDisposition.TARGET_BACKFILL,
        accounting_reemit_allowed=False,
        evidence_fingerprint="b" * 64,
    )


def _complete_report(run_id: UUID) -> ReconciliationReportV1:
    key = ComparisonKeyV1(
        tenant_id=uuid4(),
        account_id=uuid4(),
        currency="NGN",
        surface=FinancialSurface.POSITION,
        source_identity="account:NGN",
    )
    return reconcile_shadow(
        run_id=run_id,
        observed_at=datetime.now(UTC),
        observations=tuple(
            FinancialObservationV1(
                key=key,
                source=source,
                digest_sha256=_DIGEST,
            )
            for source in ObservationSource
        ),
    )


def _ready_evidence() -> CutoverEvidenceV1:
    return CutoverEvidenceV1(
        evidence_id=uuid4(),
        dispositions=(_disposition(LegacyDisposition.TARGET_BACKFILL),),
        reconciliations=tuple(_complete_report(uuid4()) for _ in range(3)),
        position_rebuild_hashes_equal=True,
        tenant_rls_canary_passed=True,
        wrong_plane_canary_passed=True,
        retirement_ratchet_exact=True,
        transport_watermark_recorded=True,
    )


def _marks() -> tuple[SourceHighWatermarkV1, ...]:
    return (
        SourceHighWatermarkV1("invoices", "updated_at:2026-08-17T10:00:00Z/id:1"),
        SourceHighWatermarkV1("settlements", "created_at:2026-08-17T10:00:00Z/id:2"),
        SourceHighWatermarkV1("allocations", "created_at:2026-08-17T10:00:00Z/id:3"),
        SourceHighWatermarkV1("integrator_checkpoint", "sequence:4001"),
    )


def test_partial_authority_switch_is_structurally_refused() -> None:
    with pytest.raises(BillingAdoptionError) as caught:
        CoupledAuthorityWatermarkV1(
            watermark_id=uuid4(),
            phase=AuthorityPhase.POST_SWITCH,
            invoice_writer=WriterState.DISABLED,
            settlement_writer=WriterState.ACTIVE,
            allocation_writer=WriterState.DISABLED,
            billing_authority=BillingAuthorityState.ACTIVE,
            source_marks=_marks(),
            recorded_at=datetime.now(UTC),
        )

    assert caught.value.code is AdoptionErrorCode.INCOHERENT_WATERMARK


def test_cutover_requires_three_distinct_complete_runs_and_every_gate() -> None:
    evidence = _ready_evidence()

    readiness = evaluate_cutover_readiness(evidence)

    assert readiness.ready
    assert readiness.blockers == ()
    assert len(readiness.evidence_fingerprint) == 64


def test_cutover_reports_every_failed_gate_without_short_circuiting() -> None:
    duplicate_run = uuid4()
    complete = _complete_report(duplicate_run)
    evidence = CutoverEvidenceV1(
        evidence_id=uuid4(),
        dispositions=(_disposition(LegacyDisposition.CUTOVER_BLOCKER),),
        reconciliations=(complete, complete, complete),
        position_rebuild_hashes_equal=False,
        tenant_rls_canary_passed=False,
        wrong_plane_canary_passed=False,
        retirement_ratchet_exact=False,
        transport_watermark_recorded=False,
    )

    readiness = evaluate_cutover_readiness(evidence)

    assert not readiness.ready
    assert readiness.blockers == (
        ReadinessBlocker.SOURCE_CLASSIFICATION_BLOCKERS,
        ReadinessBlocker.DUPLICATE_RECONCILIATION_RUN,
        ReadinessBlocker.POSITION_REBUILD_MISMATCH,
        ReadinessBlocker.TENANT_RLS_UNPROVEN,
        ReadinessBlocker.WRONG_PLANE_REFUSAL_UNPROVEN,
        ReadinessBlocker.RETIREMENT_RATCHET_DRIFT,
        ReadinessBlocker.TRANSPORT_WATERMARK_MISSING,
    )


def test_cutover_rejects_fewer_than_three_complete_reconciliations() -> None:
    ready = _ready_evidence()
    evidence = CutoverEvidenceV1(
        evidence_id=ready.evidence_id,
        dispositions=ready.dispositions,
        reconciliations=ready.reconciliations[:2],
        position_rebuild_hashes_equal=ready.position_rebuild_hashes_equal,
        tenant_rls_canary_passed=ready.tenant_rls_canary_passed,
        wrong_plane_canary_passed=ready.wrong_plane_canary_passed,
        retirement_ratchet_exact=ready.retirement_ratchet_exact,
        transport_watermark_recorded=ready.transport_watermark_recorded,
    )

    readiness = evaluate_cutover_readiness(evidence)

    assert not readiness.ready
    assert readiness.blockers == (
        ReadinessBlocker.THREE_COMPLETE_RECONCILIATIONS_REQUIRED,
    )


def test_rollback_becomes_roll_forward_after_first_billing_fact() -> None:
    before = CoupledAuthorityWatermarkV1(
        watermark_id=uuid4(),
        phase=AuthorityPhase.POST_SWITCH,
        invoice_writer=WriterState.DISABLED,
        settlement_writer=WriterState.DISABLED,
        allocation_writer=WriterState.DISABLED,
        billing_authority=BillingAuthorityState.ACTIVE,
        source_marks=_marks(),
        recorded_at=datetime.now(UTC),
    )
    after = CoupledAuthorityWatermarkV1(
        watermark_id=before.watermark_id,
        phase=before.phase,
        invoice_writer=before.invoice_writer,
        settlement_writer=before.settlement_writer,
        allocation_writer=before.allocation_writer,
        billing_authority=before.billing_authority,
        source_marks=before.source_marks,
        recorded_at=before.recorded_at,
        first_post_watermark_fact_id=uuid4(),
    )

    assert (
        rollback_disposition(before) is RollbackDisposition.TECHNICAL_ROLLBACK_ALLOWED
    )
    assert rollback_disposition(after) is RollbackDisposition.ROLL_FORWARD_REQUIRED
