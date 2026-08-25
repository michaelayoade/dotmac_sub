"""Pure readiness and rollback decisions for the coupled authority switch."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sub_billing_adoption.contracts import (
    AuthorityPhase,
    CoupledAuthorityWatermarkV1,
    DispositionResultV1,
    LegacyDisposition,
)
from sub_billing_adoption.reconciliation import ReconciliationReportV1


class ReadinessBlocker(StrEnum):
    SOURCE_CLASSIFICATION_BLOCKERS = "source_classification_blockers"
    THREE_COMPLETE_RECONCILIATIONS_REQUIRED = "three_complete_reconciliations_required"
    DUPLICATE_RECONCILIATION_RUN = "duplicate_reconciliation_run"
    POSITION_REBUILD_MISMATCH = "position_rebuild_mismatch"
    TENANT_RLS_UNPROVEN = "tenant_rls_unproven"
    WRONG_PLANE_REFUSAL_UNPROVEN = "wrong_plane_refusal_unproven"
    RETIREMENT_RATCHET_DRIFT = "retirement_ratchet_drift"
    TRANSPORT_WATERMARK_MISSING = "transport_watermark_missing"


class RollbackDisposition(StrEnum):
    TECHNICAL_ROLLBACK_ALLOWED = "technical_rollback_allowed"
    ROLL_FORWARD_REQUIRED = "roll_forward_required"


@dataclass(frozen=True, slots=True)
class CutoverEvidenceV1:
    evidence_id: UUID
    dispositions: tuple[DispositionResultV1, ...]
    reconciliations: tuple[ReconciliationReportV1, ...]
    position_rebuild_hashes_equal: bool
    tenant_rls_canary_passed: bool
    wrong_plane_canary_passed: bool
    retirement_ratchet_exact: bool
    transport_watermark_recorded: bool


@dataclass(frozen=True, slots=True)
class CutoverReadinessV1:
    evidence_id: UUID
    ready: bool
    blockers: tuple[ReadinessBlocker, ...]
    evidence_fingerprint: str


def evaluate_cutover_readiness(evidence: CutoverEvidenceV1) -> CutoverReadinessV1:
    """Evaluate evidence only; never switches authority or writes state."""

    blockers: list[ReadinessBlocker] = []
    if any(
        result.disposition
        in {
            LegacyDisposition.CUTOVER_BLOCKER,
            LegacyDisposition.KNOWN_INCORRECT_NATIVE_FACT,
        }
        for result in evidence.dispositions
    ):
        blockers.append(ReadinessBlocker.SOURCE_CLASSIFICATION_BLOCKERS)

    last_three = evidence.reconciliations[-3:]
    if len(last_three) != 3 or any(not report.complete for report in last_three):
        blockers.append(ReadinessBlocker.THREE_COMPLETE_RECONCILIATIONS_REQUIRED)
    if len({report.run_id for report in last_three}) != len(last_three):
        blockers.append(ReadinessBlocker.DUPLICATE_RECONCILIATION_RUN)
    if not evidence.position_rebuild_hashes_equal:
        blockers.append(ReadinessBlocker.POSITION_REBUILD_MISMATCH)
    if not evidence.tenant_rls_canary_passed:
        blockers.append(ReadinessBlocker.TENANT_RLS_UNPROVEN)
    if not evidence.wrong_plane_canary_passed:
        blockers.append(ReadinessBlocker.WRONG_PLANE_REFUSAL_UNPROVEN)
    if not evidence.retirement_ratchet_exact:
        blockers.append(ReadinessBlocker.RETIREMENT_RATCHET_DRIFT)
    if not evidence.transport_watermark_recorded:
        blockers.append(ReadinessBlocker.TRANSPORT_WATERMARK_MISSING)

    frozen = tuple(blockers)
    fingerprint_parts = (
        str(evidence.evidence_id),
        *(result.evidence_fingerprint for result in evidence.dispositions),
        *(report.report_fingerprint for report in evidence.reconciliations),
        *(blocker.value for blocker in frozen),
    )
    return CutoverReadinessV1(
        evidence_id=evidence.evidence_id,
        ready=not frozen,
        blockers=frozen,
        evidence_fingerprint=hashlib.sha256(
            "\x1f".join(fingerprint_parts).encode()
        ).hexdigest(),
    )


def rollback_disposition(
    watermark: CoupledAuthorityWatermarkV1,
) -> RollbackDisposition:
    """Require roll-forward after the first accepted post-watermark fact."""

    if (
        watermark.phase is AuthorityPhase.POST_SWITCH
        and watermark.first_post_watermark_fact_id is not None
    ):
        return RollbackDisposition.ROLL_FORWARD_REQUIRED
    return RollbackDisposition.TECHNICAL_ROLLBACK_ALLOWED


__all__ = [
    "CutoverEvidenceV1",
    "CutoverReadinessV1",
    "ReadinessBlocker",
    "RollbackDisposition",
    "evaluate_cutover_readiness",
    "rollback_disposition",
]
