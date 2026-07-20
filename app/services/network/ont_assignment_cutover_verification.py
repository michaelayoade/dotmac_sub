"""Read-only post-execution verification for reviewed ONT cleanup batches.

This owner snapshots exact identity-decision results plus a fresh exhaustive
assignment audit. Its only write is an immutable verification attestation; it
does not execute decisions, mutate assignments, or enable database constraints.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ont_assignment_cutover import (
    OntAssignmentCutoverVerificationAttestation,
)
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.services.network.ont_assignment_cutover import (
    OntAssignmentCutoverAudit,
    audit_ont_assignment_cutover,
)
from app.services.network.ont_assignment_cutover_batches import (
    OntAssignmentCutoverBatchError,
    OntAssignmentCutoverBatchEvidence,
    get_ont_assignment_cutover_batch_evidence,
)

STALE_CLOSED_REASON = "authoritative_assignment_identity_inputs_changed"
CONFLICT_CLOSED_REASON = "canonical_assignment_identity_conflict"


class OntAssignmentCutoverVerificationError(ValueError):
    """Raised when verification evidence or attestation input is invalid."""


class OntAssignmentCutoverVerificationBlocked(OntAssignmentCutoverVerificationError):
    """Raised when a verification preview is not eligible for attestation."""

    def __init__(self, preview: OntAssignmentCutoverVerificationPreview) -> None:
        super().__init__("ONT assignment cutover verification is blocked")
        self.preview = preview


@dataclass(frozen=True)
class OntAssignmentCutoverVerificationPreview:
    batch_id: uuid.UUID
    batch_manifest_sha256: str
    decision_evidence_sha256: str
    fresh_report_sha256: str
    evidence_payload: dict[str, object]
    evidence_sha256: str
    attestation_sha256: str
    outcome: str
    counts: dict[str, int]
    batch_scope_residual_findings: tuple[dict[str, object], ...]
    global_cutover_ready: bool
    global_blocker_assignment_count: int
    blockers: tuple[dict[str, object], ...]
    existing_attestation_id: uuid.UUID | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers and self.counts["pending"] == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "attestation_sha256": self.attestation_sha256,
            "batch_id": str(self.batch_id),
            "batch_manifest_sha256": self.batch_manifest_sha256,
            "batch_scope_residual_count": len(self.batch_scope_residual_findings),
            "batch_scope_residual_findings": list(self.batch_scope_residual_findings),
            "blockers": list(self.blockers),
            "counts": self.counts,
            "decision_evidence_sha256": self.decision_evidence_sha256,
            "evidence_payload": self.evidence_payload,
            "evidence_sha256": self.evidence_sha256,
            "existing_attestation_id": (
                str(self.existing_attestation_id)
                if self.existing_attestation_id
                else None
            ),
            "fresh_report_sha256": self.fresh_report_sha256,
            "global_blocker_assignment_count": (self.global_blocker_assignment_count),
            "global_cutover_ready": self.global_cutover_ready,
            "outcome": self.outcome,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class OntAssignmentCutoverVerificationResult:
    attestation: OntAssignmentCutoverVerificationAttestation
    created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "attestation_id": str(self.attestation.id),
            "attestation_sha256": self.attestation.attestation_sha256,
            "batch_id": str(self.attestation.proposal_batch_id),
            "created": self.created,
            "evidence_sha256": self.attestation.evidence_sha256,
            "fresh_report_sha256": self.attestation.fresh_report_sha256,
            "global_cutover_ready": self.attestation.global_cutover_ready,
            "outcome": self.attestation.outcome,
        }


@dataclass(frozen=True)
class OntAssignmentDecisionResultEvidence:
    """Canonical current result evidence for one immutable proposal batch."""

    decision_snapshots: tuple[dict[str, object], ...]
    decision_evidence_sha256: str
    counts: dict[str, int]
    blockers: tuple[dict[str, object], ...]


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OntAssignmentCutoverVerificationError(f"{field} is required")
    if len(normalized) > limit:
        raise OntAssignmentCutoverVerificationError(
            f"{field} must be at most {limit} characters"
        )
    return normalized


def _sha256(value: object, field: str) -> str:
    normalized = _required_text(value, field, limit=64).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise OntAssignmentCutoverVerificationError(
            f"{field} must be a lowercase-compatible SHA-256 digest"
        )
    return normalized


def ensure_ont_assignment_cutover_repeatable_snapshot(db: Session) -> None:
    """Require one consistent PostgreSQL snapshot for all verification reads."""

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not db.in_transaction():
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return
    isolation_level = db.connection().get_isolation_level().upper()
    if isolation_level not in {"REPEATABLE READ", "SERIALIZABLE"}:
        raise OntAssignmentCutoverVerificationError(
            "verification requires a fresh REPEATABLE READ transaction"
        )


def _timestamp(value) -> str | None:
    return value.isoformat() if value else None


def _classify_decision(decision: OntAssignmentIdentityDecision) -> str:
    if decision.status in {"proposed", "approved"}:
        return "pending"
    if decision.status == "applied":
        return "applied"
    if decision.status == "declined":
        return "declined"
    if decision.status == "closed":
        if decision.closed_reason == STALE_CLOSED_REASON:
            return "stale_closed"
        if decision.closed_reason == CONFLICT_CLOSED_REASON:
            return "conflict_closed"
        return "other_closed"
    return "other_closed"


def _decision_snapshot(
    decision: OntAssignmentIdentityDecision,
) -> dict[str, object]:
    return {
        "action": decision.action,
        "closed_reason": decision.closed_reason,
        "decision_id": str(decision.id),
        "decision_sha256": decision.decision_sha256,
        "executed_at": _timestamp(decision.executed_at),
        "executed_by": decision.executed_by,
        "input_sha256": decision.input_sha256,
        "outcome_class": _classify_decision(decision),
        "primary_assignment_id": str(decision.primary_assignment_id),
        "proposal_batch_row_number": decision.proposal_batch_row_number,
        "result_payload": decision.result_payload,
        "result_sha256": decision.result_sha256,
        "reviewed_at": _timestamp(decision.reviewed_at),
        "reviewed_by": decision.reviewed_by,
        "status": decision.status,
    }


def _result_evidence_blocker(
    decision: OntAssignmentIdentityDecision,
) -> dict[str, object] | None:
    if decision.status not in {"applied", "closed"}:
        return None
    if decision.result_payload is None or decision.result_sha256 is None:
        return {
            "code": "terminal_decision_result_missing",
            "decision_id": str(decision.id),
        }
    actual = _digest(decision.result_payload)
    if actual != decision.result_sha256:
        return {
            "actual_result_sha256": actual,
            "code": "terminal_decision_result_digest_mismatch",
            "decision_id": str(decision.id),
            "stored_result_sha256": decision.result_sha256,
        }
    return None


def _manifest_scope(evidence: OntAssignmentCutoverBatchEvidence) -> set[str]:
    items = evidence.batch.manifest_payload.get("items")
    if not isinstance(items, list):
        raise OntAssignmentCutoverVerificationError(
            "stored cutover batch manifest items are invalid"
        )
    assignment_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("repair"), dict):
            raise OntAssignmentCutoverVerificationError(
                "stored cutover batch manifest repair is invalid"
            )
        repair = cast(dict[str, object], item["repair"])
        assignment_ids.add(str(item.get("assignment_id")))
        duplicate_ids = repair.get("duplicate_assignment_ids")
        if not isinstance(duplicate_ids, list):
            raise OntAssignmentCutoverVerificationError(
                "stored cutover batch conflict IDs are invalid"
            )
        assignment_ids.update(str(value) for value in duplicate_ids)
    return assignment_ids


def _residual_findings(
    audit: OntAssignmentCutoverAudit,
    scope_assignment_ids: set[str],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "assignment_id": str(finding.assignment_id),
            "finding_sha256": finding.input_sha256,
            "reason_codes": list(finding.reason_codes),
            "related_assignment_ids": [
                str(value) for value in finding.related_assignment_ids
            ],
        }
        for finding in audit.findings
        if str(finding.assignment_id) in scope_assignment_ids
    )


def _counts(
    decisions: Sequence[OntAssignmentIdentityDecision],
) -> dict[str, int]:
    counts = {
        "pending": 0,
        "applied": 0,
        "declined": 0,
        "stale_closed": 0,
        "conflict_closed": 0,
        "other_closed": 0,
    }
    for decision in decisions:
        counts[_classify_decision(decision)] += 1
    return counts


def snapshot_ont_assignment_cutover_decision_results(
    decisions: Sequence[OntAssignmentIdentityDecision],
) -> OntAssignmentDecisionResultEvidence:
    """Return the single canonical digest and validation of current results."""

    snapshots = tuple(_decision_snapshot(decision) for decision in decisions)
    blockers = tuple(
        blocker
        for decision in decisions
        if (blocker := _result_evidence_blocker(decision)) is not None
    )
    return OntAssignmentDecisionResultEvidence(
        decision_snapshots=snapshots,
        decision_evidence_sha256=_digest(snapshots),
        counts=_counts(decisions),
        blockers=blockers,
    )


def _outcome(
    *,
    review_action: str | None,
    counts: dict[str, int],
    residual_count: int,
) -> str:
    if review_action is None:
        return "awaiting_review"
    if counts["pending"]:
        return "pending"
    if review_action == "decline":
        return "declined"
    if counts["stale_closed"]:
        return "completed_with_stale_closures"
    if counts["conflict_closed"]:
        return "completed_with_conflict_closures"
    if counts["other_closed"]:
        return "completed_with_other_closures"
    if residual_count:
        return "applied_with_residual_findings"
    return "applied_clean_scope"


def _existing_attestation(
    db: Session,
    *,
    batch_id: uuid.UUID,
    evidence_sha256: str,
) -> OntAssignmentCutoverVerificationAttestation | None:
    return db.scalar(
        select(OntAssignmentCutoverVerificationAttestation).where(
            OntAssignmentCutoverVerificationAttestation.proposal_batch_id == batch_id,
            OntAssignmentCutoverVerificationAttestation.evidence_sha256
            == evidence_sha256,
        )
    )


def preview_ont_assignment_cutover_verification(
    db: Session,
    batch_id: object,
    *,
    expected_manifest_sha256: object,
    verified_by: object,
    verification_notes: object,
) -> OntAssignmentCutoverVerificationPreview:
    """Build exact terminal-result and fresh-audit evidence without writing."""

    expected_manifest = _sha256(expected_manifest_sha256, "expected_manifest_sha256")
    ensure_ont_assignment_cutover_repeatable_snapshot(db)
    actor = _required_text(verified_by, "verified_by", limit=160)
    notes = _required_text(verification_notes, "verification_notes", limit=4000)
    try:
        batch_evidence = get_ont_assignment_cutover_batch_evidence(db, batch_id)
    except OntAssignmentCutoverBatchError as exc:
        raise OntAssignmentCutoverVerificationError(str(exc)) from exc
    batch = batch_evidence.batch
    review = batch_evidence.review
    decisions = batch_evidence.decisions
    if batch.manifest_sha256 != expected_manifest:
        raise OntAssignmentCutoverVerificationError(
            "cutover batch manifest differs from the expected digest"
        )
    blockers: list[dict[str, object]] = []
    if review is None:
        blockers.append({"code": "batch_review_missing"})
    elif review.batch_manifest_sha256 != batch.manifest_sha256:
        blockers.append({"code": "batch_review_manifest_mismatch"})

    forbidden_actors = {batch.proposed_by}
    if review is not None:
        forbidden_actors.add(review.reviewed_by)
    forbidden_actors.update(
        decision.executed_by for decision in decisions if decision.executed_by
    )
    if actor in forbidden_actors:
        blockers.append(
            {
                "code": "verification_actor_not_independent",
                "verified_by": actor,
            }
        )

    decision_evidence = snapshot_ont_assignment_cutover_decision_results(decisions)
    decision_snapshots = decision_evidence.decision_snapshots
    decision_evidence_sha256 = decision_evidence.decision_evidence_sha256
    counts = decision_evidence.counts
    if counts["pending"]:
        blockers.append(
            {
                "code": "batch_decisions_pending",
                "decision_ids": [
                    str(decision.id)
                    for decision in decisions
                    if _classify_decision(decision) == "pending"
                ],
                "pending_count": counts["pending"],
            }
        )
    if review is not None and review.action == "decline":
        if counts["declined"] != batch.item_count:
            blockers.append({"code": "declined_batch_decision_state_mismatch"})
    elif review is not None and review.action == "approve" and counts["declined"]:
        blockers.append({"code": "approved_batch_contains_declined_decision"})

    blockers.extend(decision_evidence.blockers)

    audit = audit_ont_assignment_cutover(db)
    residual_findings = _residual_findings(audit, _manifest_scope(batch_evidence))
    outcome = _outcome(
        review_action=review.action if review else None,
        counts=counts,
        residual_count=len(residual_findings),
    )
    evidence_payload: dict[str, object] = {
        "batch": {
            "id": str(batch.id),
            "item_count": batch.item_count,
            "manifest_sha256": batch.manifest_sha256,
            "proposal_report_sha256": batch.report_sha256,
        },
        "batch_scope_residual_findings": list(residual_findings),
        "counts": counts,
        "decision_evidence_sha256": decision_evidence_sha256,
        "decisions": list(decision_snapshots),
        "fresh_audit": {
            "active_assignment_count": audit.active_assignment_count,
            "blocker_assignment_count": audit.blocker_assignment_count,
            "gates": [gate.to_dict() for gate in audit.gates],
            "ready_for_constraints": audit.ready_for_constraints,
            "reason_counts": audit.reason_counts,
            "report_sha256": audit.report_sha256,
        },
        "outcome": outcome,
        "review": (
            {
                "action": review.action,
                "attestation_sha256": review.attestation_sha256,
                "id": str(review.id),
                "reviewed_by": review.reviewed_by,
            }
            if review
            else None
        ),
        "schema_version": 1,
    }
    evidence_sha256 = _digest(evidence_payload)
    attestation_payload = {
        "batch_id": str(batch.id),
        "evidence_sha256": evidence_sha256,
        "schema_version": 1,
        "verification_notes": notes,
        "verified_by": actor,
    }
    attestation_sha256 = _digest(attestation_payload)
    existing = _existing_attestation(
        db, batch_id=batch.id, evidence_sha256=evidence_sha256
    )
    if existing is not None and existing.attestation_sha256 != attestation_sha256:
        blockers.append(
            {
                "attestation_id": str(existing.id),
                "code": "evidence_already_attested_differently",
            }
        )
    return OntAssignmentCutoverVerificationPreview(
        batch_id=batch.id,
        batch_manifest_sha256=batch.manifest_sha256,
        decision_evidence_sha256=decision_evidence_sha256,
        fresh_report_sha256=audit.report_sha256,
        evidence_payload=evidence_payload,
        evidence_sha256=evidence_sha256,
        attestation_sha256=attestation_sha256,
        outcome=outcome,
        counts=counts,
        batch_scope_residual_findings=residual_findings,
        global_cutover_ready=audit.ready_for_constraints,
        global_blocker_assignment_count=audit.blocker_assignment_count,
        blockers=tuple(blockers),
        existing_attestation_id=existing.id if existing else None,
    )


def attest_ont_assignment_cutover_verification(
    db: Session,
    batch_id: object,
    *,
    expected_manifest_sha256: object,
    expected_evidence_sha256: object,
    verified_by: object,
    verification_notes: object,
) -> OntAssignmentCutoverVerificationResult:
    """Persist one immutable exact snapshot without changing source state."""

    expected_evidence = _sha256(expected_evidence_sha256, "expected_evidence_sha256")
    preview = preview_ont_assignment_cutover_verification(
        db,
        batch_id,
        expected_manifest_sha256=expected_manifest_sha256,
        verified_by=verified_by,
        verification_notes=verification_notes,
    )
    if preview.evidence_sha256 != expected_evidence:
        raise OntAssignmentCutoverVerificationError(
            "verification evidence changed after preview"
        )
    if not preview.ready:
        raise OntAssignmentCutoverVerificationBlocked(preview)
    if preview.existing_attestation_id is not None:
        existing = db.get(
            OntAssignmentCutoverVerificationAttestation,
            preview.existing_attestation_id,
        )
        if existing is None:
            raise OntAssignmentCutoverVerificationError(
                "existing verification attestation disappeared"
            )
        return OntAssignmentCutoverVerificationResult(
            attestation=existing, created=False
        )

    batch_evidence = get_ont_assignment_cutover_batch_evidence(db, batch_id)
    review = batch_evidence.review
    if review is None:
        raise OntAssignmentCutoverVerificationError("batch review disappeared")
    counts = preview.counts
    attestation = OntAssignmentCutoverVerificationAttestation(
        proposal_batch_id=batch_evidence.batch.id,
        batch_review_id=review.id,
        batch_manifest_sha256=preview.batch_manifest_sha256,
        decision_evidence_sha256=preview.decision_evidence_sha256,
        fresh_report_sha256=preview.fresh_report_sha256,
        evidence_payload=preview.evidence_payload,
        evidence_sha256=preview.evidence_sha256,
        outcome=preview.outcome,
        item_count=batch_evidence.batch.item_count,
        pending_count=counts["pending"],
        applied_count=counts["applied"],
        declined_count=counts["declined"],
        stale_closed_count=counts["stale_closed"],
        conflict_closed_count=counts["conflict_closed"],
        other_closed_count=counts["other_closed"],
        batch_scope_residual_count=len(preview.batch_scope_residual_findings),
        global_blocker_assignment_count=(preview.global_blocker_assignment_count),
        global_cutover_ready=preview.global_cutover_ready,
        verified_by=_required_text(verified_by, "verified_by", limit=160),
        verification_notes=_required_text(
            verification_notes, "verification_notes", limit=4000
        ),
        attestation_sha256=preview.attestation_sha256,
    )
    try:
        with db.begin_nested():
            db.add(attestation)
            db.flush()
        db.commit()
        db.refresh(attestation)
        return OntAssignmentCutoverVerificationResult(
            attestation=attestation, created=True
        )
    except IntegrityError:
        existing = _existing_attestation(
            db,
            batch_id=batch_evidence.batch.id,
            evidence_sha256=preview.evidence_sha256,
        )
        if (
            existing is not None
            and existing.attestation_sha256 == preview.attestation_sha256
        ):
            return OntAssignmentCutoverVerificationResult(
                attestation=existing, created=False
            )
        raise


def inspect_ont_assignment_cutover_verifications(
    db: Session, batch_id: object
) -> dict[str, object]:
    try:
        batch_evidence = get_ont_assignment_cutover_batch_evidence(db, batch_id)
    except OntAssignmentCutoverBatchError as exc:
        raise OntAssignmentCutoverVerificationError(str(exc)) from exc
    rows = tuple(
        db.scalars(
            select(OntAssignmentCutoverVerificationAttestation)
            .where(
                OntAssignmentCutoverVerificationAttestation.proposal_batch_id
                == batch_evidence.batch.id
            )
            .order_by(OntAssignmentCutoverVerificationAttestation.verified_at.desc())
        )
    )
    return {
        "batch_id": str(batch_evidence.batch.id),
        "manifest_sha256": batch_evidence.batch.manifest_sha256,
        "verification_attestations": [
            {
                "attestation_sha256": row.attestation_sha256,
                "batch_scope_residual_count": row.batch_scope_residual_count,
                "counts": {
                    "applied": row.applied_count,
                    "conflict_closed": row.conflict_closed_count,
                    "declined": row.declined_count,
                    "other_closed": row.other_closed_count,
                    "pending": row.pending_count,
                    "stale_closed": row.stale_closed_count,
                },
                "evidence_sha256": row.evidence_sha256,
                "fresh_report_sha256": row.fresh_report_sha256,
                "global_blocker_assignment_count": (
                    row.global_blocker_assignment_count
                ),
                "global_cutover_ready": row.global_cutover_ready,
                "id": str(row.id),
                "outcome": row.outcome,
                "verification_notes": row.verification_notes,
                "verified_at": row.verified_at.isoformat(),
                "verified_by": row.verified_by,
            }
            for row in rows
        ],
    }


__all__ = [
    "CONFLICT_CLOSED_REASON",
    "STALE_CLOSED_REASON",
    "OntAssignmentCutoverVerificationBlocked",
    "OntAssignmentDecisionResultEvidence",
    "OntAssignmentCutoverVerificationError",
    "OntAssignmentCutoverVerificationPreview",
    "OntAssignmentCutoverVerificationResult",
    "attest_ont_assignment_cutover_verification",
    "ensure_ont_assignment_cutover_repeatable_snapshot",
    "inspect_ont_assignment_cutover_verifications",
    "preview_ont_assignment_cutover_verification",
    "snapshot_ont_assignment_cutover_decision_results",
]
