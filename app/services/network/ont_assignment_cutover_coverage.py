"""Read-only reconciliation of current ONT cleanup findings to reviewed lineage.

This owner joins the exhaustive assignment audit to immutable proposal, review,
decision-result, and verification evidence in one repeatable snapshot. It reports
coverage and conservative readiness gates only; it cannot execute repairs or
authorize, create, validate, or enable database constraints.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ont_assignment_cutover import (
    OntAssignmentCutoverProposalBatch,
    OntAssignmentCutoverVerificationAttestation,
)
from app.services.network.ont_assignment_cutover import (
    OntAssignmentCutoverFinding,
    audit_ont_assignment_cutover,
)
from app.services.network.ont_assignment_cutover_batches import (
    OntAssignmentCutoverBatchError,
    get_ont_assignment_cutover_batch_evidence,
)
from app.services.network.ont_assignment_cutover_verification import (
    OntAssignmentDecisionResultEvidence,
    ensure_ont_assignment_cutover_repeatable_snapshot,
    snapshot_ont_assignment_cutover_decision_results,
)

COVERAGE_STATES = (
    "unassigned",
    "superseded_evidence",
    "ambiguous_overlapping_coverage",
    "exact_pending_review",
    "exact_pending_execution",
    "exact_applied_residual",
    "exact_declined",
    "exact_stale_closed",
    "exact_conflict_closed",
    "exact_other_closed",
)
VERIFICATION_STATES = (
    "missing",
    "current",
    "superseded_report",
    "decision_drift",
)


class OntAssignmentCutoverCoverageError(ValueError):
    """Raised when immutable historical lineage cannot be reconciled."""


@dataclass(frozen=True)
class OntAssignmentCutoverCoverageReport:
    cutover_report_sha256: str
    coverage_report_sha256: str
    current_findings: tuple[dict[str, object], ...]
    lineages: tuple[dict[str, object], ...]
    coverage_counts: dict[str, int]
    decision_counts: dict[str, int]
    batch_verification_counts: dict[str, int]
    decision_result_blockers: tuple[dict[str, object], ...]
    gates: tuple[dict[str, object], ...]
    ready_for_constraint_authorization_review: bool
    audit: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "audit": self.audit,
            "batch_verification_counts": self.batch_verification_counts,
            "coverage_counts": self.coverage_counts,
            "coverage_report_sha256": self.coverage_report_sha256,
            "current_findings": list(self.current_findings),
            "cutover_report_sha256": self.cutover_report_sha256,
            "decision_counts": self.decision_counts,
            "decision_result_blockers": list(self.decision_result_blockers),
            "gates": list(self.gates),
            "lineages": list(self.lineages),
            "ready_for_constraint_authorization_review": (
                self.ready_for_constraint_authorization_review
            ),
            "schema_version": 1,
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value) -> str | None:
    return value.isoformat() if value else None


def _manifest_items(payload: dict[str, object]) -> list[dict[str, object]]:
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise OntAssignmentCutoverCoverageError(
            "stored cutover batch manifest items are invalid"
        )
    return cast(list[dict[str, object]], items)


def _repair_scope(item: dict[str, object]) -> tuple[str, ...]:
    repair = item.get("repair")
    if not isinstance(repair, dict):
        raise OntAssignmentCutoverCoverageError(
            "stored cutover batch repair evidence is invalid"
        )
    duplicate_ids = repair.get("duplicate_assignment_ids")
    if not isinstance(duplicate_ids, list):
        raise OntAssignmentCutoverCoverageError(
            "stored cutover batch repair scope is invalid"
        )
    return tuple(
        sorted({str(item["assignment_id"]), *(str(value) for value in duplicate_ids)})
    )


def _verification_state(
    attestations: tuple[OntAssignmentCutoverVerificationAttestation, ...],
    *,
    manifest_sha256: str,
    decision_evidence_sha256: str,
    current_report_sha256: str,
) -> tuple[str, tuple[str, ...]]:
    if not attestations:
        return "missing", ()
    matching_decision = tuple(
        row
        for row in attestations
        if row.batch_manifest_sha256 == manifest_sha256
        and row.decision_evidence_sha256 == decision_evidence_sha256
    )
    current = tuple(
        row
        for row in matching_decision
        if row.fresh_report_sha256 == current_report_sha256
    )
    if current:
        return "current", tuple(str(row.id) for row in current)
    if matching_decision:
        return "superseded_report", tuple(str(row.id) for row in matching_decision)
    return "decision_drift", tuple(str(row.id) for row in attestations)


def _exact_coverage_state(lineage: dict[str, object]) -> str:
    status = lineage["decision_status"]
    if status == "proposed":
        return "exact_pending_review"
    if status == "approved":
        return "exact_pending_execution"
    if status == "applied":
        return "exact_applied_residual"
    if status == "declined":
        return "exact_declined"
    outcome_class = lineage["decision_outcome_class"]
    if outcome_class == "stale_closed":
        return "exact_stale_closed"
    if outcome_class == "conflict_closed":
        return "exact_conflict_closed"
    return "exact_other_closed"


def _finding_projection(
    finding: OntAssignmentCutoverFinding,
    *,
    lineages_by_scope: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    assignment_id = str(finding.assignment_id)
    finding_sha256 = finding.input_sha256
    candidates = lineages_by_scope.get(assignment_id, [])
    exact = [
        row
        for row in candidates
        if row["primary_assignment_id"] == assignment_id
        and row["proposal_finding_sha256"] == finding_sha256
    ]
    non_exact = [row for row in candidates if row not in exact]
    if len(exact) > 1 or (not exact and len(non_exact) > 1):
        coverage_state = "ambiguous_overlapping_coverage"
    elif len(exact) == 1:
        coverage_state = _exact_coverage_state(exact[0])
    elif len(non_exact) == 1:
        coverage_state = "superseded_evidence"
    else:
        coverage_state = "unassigned"
    payload = finding.to_dict()
    payload.update(
        {
            "coverage_state": coverage_state,
            "exact_lineage_ids": [str(row["lineage_id"]) for row in exact],
            "scope_lineage_ids": [str(row["lineage_id"]) for row in non_exact],
        }
    )
    return payload


def _counts(values: list[str], known: tuple[str, ...]) -> dict[str, int]:
    counter = Counter(values)
    return {name: counter[name] for name in known}


def _gate(code: str, ready: bool, detail: str) -> dict[str, object]:
    return {"code": code, "detail": detail, "ready": ready}


def reconcile_ont_assignment_cutover_coverage(
    db: Session,
) -> OntAssignmentCutoverCoverageReport:
    """Reconcile current findings and all immutable cleanup lineage without writes."""

    ensure_ont_assignment_cutover_repeatable_snapshot(db)
    audit = audit_ont_assignment_cutover(db)
    batches = tuple(
        db.scalars(
            select(OntAssignmentCutoverProposalBatch).order_by(
                OntAssignmentCutoverProposalBatch.created_at,
                OntAssignmentCutoverProposalBatch.id,
            )
        )
    )
    attestation_rows = tuple(
        db.scalars(
            select(OntAssignmentCutoverVerificationAttestation).order_by(
                OntAssignmentCutoverVerificationAttestation.verified_at,
                OntAssignmentCutoverVerificationAttestation.id,
            )
        )
    )
    attestations_by_batch: dict[
        str, list[OntAssignmentCutoverVerificationAttestation]
    ] = defaultdict(list)
    for row in attestation_rows:
        attestations_by_batch[str(row.proposal_batch_id)].append(row)

    current_finding_ids = {str(finding.assignment_id) for finding in audit.findings}
    lineages: list[dict[str, object]] = []
    lineages_by_scope: dict[str, list[dict[str, object]]] = defaultdict(list)
    result_blockers: list[dict[str, object]] = []
    decision_states: list[str] = []
    verification_states: list[str] = []
    applied_batch_verification: list[str] = []

    for batch in batches:
        try:
            evidence = get_ont_assignment_cutover_batch_evidence(db, batch.id)
        except OntAssignmentCutoverBatchError as exc:
            raise OntAssignmentCutoverCoverageError(str(exc)) from exc
        decision_evidence: OntAssignmentDecisionResultEvidence = (
            snapshot_ont_assignment_cutover_decision_results(evidence.decisions)
        )
        for blocker in decision_evidence.blockers:
            result_blockers.append({"batch_id": str(batch.id), **blocker})
        attestations = tuple(attestations_by_batch[str(batch.id)])
        verification_state, matching_attestation_ids = _verification_state(
            attestations,
            manifest_sha256=batch.manifest_sha256,
            decision_evidence_sha256=decision_evidence.decision_evidence_sha256,
            current_report_sha256=audit.report_sha256,
        )
        verification_states.append(verification_state)
        if any(decision.status == "applied" for decision in evidence.decisions):
            applied_batch_verification.append(verification_state)

        items = _manifest_items(batch.manifest_payload)
        for item, decision, snapshot in zip(
            items,
            evidence.decisions,
            decision_evidence.decision_snapshots,
            strict=True,
        ):
            scope = _repair_scope(item)
            decision_states.append(str(snapshot["outcome_class"]))
            lineage: dict[str, object] = {
                "batch_created_at": _timestamp(batch.created_at),
                "batch_id": str(batch.id),
                "batch_manifest_sha256": batch.manifest_sha256,
                "batch_report_sha256": batch.report_sha256,
                "decision_closed_reason": decision.closed_reason,
                "decision_id": str(decision.id),
                "decision_outcome_class": snapshot["outcome_class"],
                "decision_result_sha256": decision.result_sha256,
                "decision_status": decision.status,
                "lineage_id": f"{batch.id}:{item['row_number']}",
                "matching_verification_attestation_ids": list(matching_attestation_ids),
                "primary_assignment_id": str(item["assignment_id"]),
                "proposal_finding_sha256": str(item["finding_sha256"]),
                "repair_scope_assignment_ids": list(scope),
                "review": (
                    {
                        "action": evidence.review.action,
                        "attestation_sha256": evidence.review.attestation_sha256,
                        "reviewed_at": _timestamp(evidence.review.reviewed_at),
                        "reviewed_by": evidence.review.reviewed_by,
                    }
                    if evidence.review
                    else None
                ),
                "row_number": item["row_number"],
                "scope_state": (
                    "residual" if current_finding_ids.intersection(scope) else "clean"
                ),
                "verification_state": verification_state,
            }
            lineages.append(lineage)
            for assignment_id in scope:
                lineages_by_scope[assignment_id].append(lineage)

    current_findings = tuple(
        _finding_projection(finding, lineages_by_scope=lineages_by_scope)
        for finding in audit.findings
    )
    coverage_counts = _counts(
        [str(row["coverage_state"]) for row in current_findings], COVERAGE_STATES
    )
    decision_count_names = (
        "pending",
        "applied",
        "declined",
        "stale_closed",
        "conflict_closed",
        "other_closed",
    )
    decision_counts = _counts(decision_states, decision_count_names)
    batch_verification_counts = _counts(verification_states, VERIFICATION_STATES)
    exact_once = all(
        len(cast(list[object], row["exact_lineage_ids"])) == 1
        for row in current_findings
    )
    no_pending = decision_counts["pending"] == 0
    applied_verified = all(state == "current" for state in applied_batch_verification)
    no_decision_drift = batch_verification_counts["decision_drift"] == 0
    result_evidence_valid = not result_blockers
    gates = (
        _gate(
            "exhaustive_assignment_audit_clean",
            audit.ready_for_constraints,
            "Every authoritative assignment invariant gate is clean.",
        ),
        _gate(
            "current_findings_exactly_assigned_once",
            exact_once,
            "Every current finding maps to exactly one unchanged primary lineage.",
        ),
        _gate(
            "no_pending_cleanup_decisions",
            no_pending,
            "No cleanup decision remains proposed or approved.",
        ),
        _gate(
            "terminal_decision_result_evidence_valid",
            result_evidence_valid,
            "Every applied or closed decision has intact canonical result evidence.",
        ),
        _gate(
            "all_applied_batches_currently_verified",
            applied_verified,
            "Every batch containing an applied decision has a verification attestation for this audit snapshot.",
        ),
        _gate(
            "no_verification_decision_drift",
            no_decision_drift,
            "No attested batch differs from its current canonical decision-result evidence.",
        ),
    )
    ready = all(bool(gate["ready"]) for gate in gates)
    audit_payload = {
        "active_assignment_count": audit.active_assignment_count,
        "blocker_assignment_count": audit.blocker_assignment_count,
        "gates": [gate.to_dict() for gate in audit.gates],
        "ready_for_constraints": audit.ready_for_constraints,
        "reason_counts": audit.reason_counts,
        "report_sha256": audit.report_sha256,
    }
    report_payload: dict[str, object] = {
        "audit": audit_payload,
        "batch_verification_counts": batch_verification_counts,
        "coverage_counts": coverage_counts,
        "current_findings": list(current_findings),
        "cutover_report_sha256": audit.report_sha256,
        "decision_counts": decision_counts,
        "decision_result_blockers": result_blockers,
        "gates": list(gates),
        "lineages": lineages,
        "ready_for_constraint_authorization_review": ready,
        "schema_version": 1,
    }
    return OntAssignmentCutoverCoverageReport(
        cutover_report_sha256=audit.report_sha256,
        coverage_report_sha256=_digest(report_payload),
        current_findings=current_findings,
        lineages=tuple(lineages),
        coverage_counts=coverage_counts,
        decision_counts=decision_counts,
        batch_verification_counts=batch_verification_counts,
        decision_result_blockers=tuple(result_blockers),
        gates=gates,
        ready_for_constraint_authorization_review=ready,
        audit=audit_payload,
    )


__all__ = [
    "COVERAGE_STATES",
    "VERIFICATION_STATES",
    "OntAssignmentCutoverCoverageError",
    "OntAssignmentCutoverCoverageReport",
    "reconcile_ont_assignment_cutover_coverage",
]
