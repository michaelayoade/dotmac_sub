"""Read-only coverage reconciliation for the complete staged path cohort.

This owner derives coverage, lifecycle, canonical-mutation, and provenance
evidence from staged source facts and reviewed connectivity lineage. It never
selects endpoints from geometry, creates proposals, advances decisions,
approves change requests, or mutates canonical topology.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_topology_connectivity import (
    FiberTopologyConnectivityDecision,
    FiberTopologySegmentSourceLink,
    FiberTopologyTerminationResolution,
)
from app.models.fiber_topology_connectivity_review import (
    FiberTopologyConnectivityBatchReview,
    FiberTopologyConnectivityProposalBatch,
    FiberTopologyConnectivityRun,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import FiberSegment
from app.services.network.fiber_topology_field_observations import (
    field_verification_state_counts,
    project_field_verification_evidence,
)

COVERAGE_STATES = (
    "source_blocked",
    "unassigned",
    "superseded_evidence",
    "ambiguous_overlapping_coverage",
    "exact",
)
LIFECYCLE_STATES = (
    "source_identity_blocked",
    "missing_endpoint_evidence",
    "stale_source_evidence",
    "overlapping_decision_evidence",
    "missing_batch_evidence",
    "batch_evidence_drift",
    "pending_review",
    "declined",
    "pending_execution",
    "pending_endpoint_mutation",
    "pending_segment_mutation",
    "execution_evidence_drift",
    "applied_current",
    "provenance_drift",
    "rejected_current",
    "stale_closed",
    "change_rejected",
    "other_closed",
)
ACTIVE_DECISION_STATUSES = {
    "proposed",
    "approved",
    "endpoint_change_requested",
    "segment_change_requested",
}
EXECUTED_DECISION_STATUSES = {
    "endpoint_change_requested",
    "segment_change_requested",
    "applied",
    "closed",
}
RUN_BLOCKER_CODES = {"run_outcomes_invalid", "run_result_evidence_mismatch"}
STALE_CLOSED_REASONS = {
    "source_or_endpoint_changed_before_execution",
    "source_changed_before_segment_request",
}
CHANGE_REJECTED_REASONS = {
    "endpoint_change_request_rejected",
    "segment_change_request_rejected",
}


class FiberTopologyConnectivityCoverageError(ValueError):
    """Raised when coverage cannot be derived from one consistent snapshot."""


@dataclass(frozen=True)
class FiberTopologyConnectivityCoverageReport:
    coverage_report_sha256: str
    staged_path_count: int
    source_batch_count: int
    paths: tuple[dict[str, object], ...]
    lineages: tuple[dict[str, object], ...]
    coverage_counts: dict[str, int]
    lifecycle_counts: dict[str, int]
    decision_counts: dict[str, int]
    field_verification_counts: dict[str, int]
    batch_evidence_blockers: tuple[dict[str, object], ...]
    gates: tuple[dict[str, object], ...]
    ready_for_connectivity_cutover_review: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_evidence_blockers": list(self.batch_evidence_blockers),
            "coverage_counts": self.coverage_counts,
            "coverage_report_sha256": self.coverage_report_sha256,
            "decision_counts": self.decision_counts,
            "field_verification_counts": self.field_verification_counts,
            "gates": list(self.gates),
            "lifecycle_counts": self.lifecycle_counts,
            "lineages": list(self.lineages),
            "paths": list(self.paths),
            "ready_for_connectivity_cutover_review": (
                self.ready_for_connectivity_cutover_review
            ),
            "schema_version": 1,
            "source_batch_count": self.source_batch_count,
            "staged_path_count": self.staged_path_count,
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value) -> str | None:
    return value.isoformat() if value else None


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _counts(values: list[str], known: tuple[str, ...]) -> dict[str, int]:
    counter = Counter(values)
    return {name: counter[name] for name in known}


def _gate(code: str, ready: bool, detail: str) -> dict[str, object]:
    return {"code": code, "detail": detail, "ready": ready}


def ensure_connectivity_coverage_repeatable_snapshot(db: Session) -> None:
    """Require one consistent PostgreSQL snapshot for every exhaustive read."""

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not db.in_transaction():
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return
    isolation_level = db.connection().get_isolation_level().upper()
    if isolation_level not in {"REPEATABLE READ", "SERIALIZABLE"}:
        raise FiberTopologyConnectivityCoverageError(
            "coverage requires a fresh REPEATABLE READ transaction"
        )


def _source_key_from_feature(
    feature: FiberTopologyStagedFeature,
) -> tuple[str, str, str]:
    identity = feature.external_id or f"feature:{feature.id}"
    return feature.batch.source_system, feature.asset_type, identity


def _source_key_from_decision(
    decision: FiberTopologyConnectivityDecision,
) -> tuple[str, str, str]:
    return (
        decision.source_system,
        decision.source_asset_type,
        decision.source_external_id,
    )


def _latest_staged_paths(db: Session) -> list[FiberTopologyStagedFeature]:
    rows = list(
        db.scalars(
            select(FiberTopologyStagedFeature)
            .join(FiberTopologyStagedFeature.batch)
            .options(joinedload(FiberTopologyStagedFeature.batch))
            .where(FiberTopologyStagedFeature.asset_type == "fiber_segment")
            .order_by(
                FiberTopologySourceBatch.created_at.desc(),
                FiberTopologyStagedFeature.created_at.desc(),
                FiberTopologyStagedFeature.id.desc(),
            )
        )
        .unique()
        .all()
    )
    latest: list[FiberTopologyStagedFeature] = []
    seen: set[tuple[str, str, str]] = set()
    for feature in rows:
        source_key = _source_key_from_feature(feature)
        if source_key in seen:
            continue
        seen.add(source_key)
        latest.append(feature)
    return sorted(latest, key=_source_key_from_feature)


def _decision_manifest_item(
    decision: FiberTopologyConnectivityDecision, row_number: int
) -> dict[str, object]:
    return {
        "action": decision.action,
        "cable_type": decision.cable_type,
        "decision_sha256": decision.decision_sha256,
        "end_endpoint_ref_id": (
            str(decision.end_endpoint_ref_id) if decision.end_endpoint_ref_id else None
        ),
        "end_endpoint_type": decision.end_endpoint_type,
        "feature_content_sha256": decision.feature_content_sha256,
        "fiber_count": decision.fiber_count,
        "length_m": decision.length_m,
        "proposed_by": decision.proposed_by,
        "reason": decision.reason,
        "row_number": row_number,
        "segment_type": decision.segment_type,
        "source_asset_type": decision.source_asset_type,
        "source_external_id": decision.source_external_id,
        "source_system": decision.source_system,
        "staged_feature_id": str(decision.staged_feature_id),
        "start_endpoint_ref_id": (
            str(decision.start_endpoint_ref_id)
            if decision.start_endpoint_ref_id
            else None
        ),
        "start_endpoint_type": decision.start_endpoint_type,
        "target_segment_id": (
            str(decision.target_segment_id) if decision.target_segment_id else None
        ),
    }


def _manifest_items(batch: FiberTopologyConnectivityProposalBatch) -> list[dict]:
    items = batch.manifest_payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise FiberTopologyConnectivityCoverageError(
            "stored connectivity batch manifest items are invalid"
        )
    return cast(list[dict], items)


def _review_attestation_sha256(
    batch: FiberTopologyConnectivityProposalBatch,
    review: FiberTopologyConnectivityBatchReview,
) -> str:
    return _digest(
        {
            "action": review.action,
            "batch_id": str(batch.id),
            "batch_manifest_sha256": review.batch_manifest_sha256,
            "item_count": review.item_count,
            "proposed_by": review.proposed_by,
            "review_notes": review.review_notes,
            "reviewed_by": review.reviewed_by,
            "schema_version": 1,
        }
    )


def _batch_blockers(
    batch: FiberTopologyConnectivityProposalBatch,
    decisions: list[FiberTopologyConnectivityDecision],
    review: FiberTopologyConnectivityBatchReview | None,
    runs: list[FiberTopologyConnectivityRun],
) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    try:
        items = _manifest_items(batch)
    except FiberTopologyConnectivityCoverageError as exc:
        return [{"code": "invalid_manifest_items", "message": str(exc)}]
    if _digest(batch.manifest_payload) != batch.manifest_sha256:
        blockers.append(
            {
                "code": "manifest_digest_mismatch",
                "message": "stored manifest payload does not match its SHA-256",
            }
        )
    if batch.manifest_payload.get("request_sha256") != batch.request_sha256:
        blockers.append(
            {
                "code": "request_digest_reference_mismatch",
                "message": "manifest request digest does not match the batch",
            }
        )
    if len(items) != batch.item_count or len(decisions) != batch.item_count:
        blockers.append(
            {
                "code": "batch_item_count_mismatch",
                "message": "manifest and delegated decision counts differ",
            }
        )
    else:
        for row_number, (item, decision) in enumerate(
            zip(items, decisions, strict=True), start=1
        ):
            if (
                decision.proposal_batch_row_number != row_number
                or item != _decision_manifest_item(decision, row_number)
            ):
                blockers.append(
                    {
                        "code": "batch_row_evidence_mismatch",
                        "decision_id": str(decision.id),
                        "message": f"batch row {row_number} differs from its decision",
                        "row_number": row_number,
                    }
                )
    if review:
        if (
            review.batch_manifest_sha256 != batch.manifest_sha256
            or review.proposal_batch_id != batch.id
            or review.proposed_by != batch.proposed_by
            or review.item_count != batch.item_count
            or _review_attestation_sha256(batch, review) != review.attestation_sha256
        ):
            blockers.append(
                {
                    "code": "batch_review_attestation_mismatch",
                    "message": "stored batch review does not match its exact manifest",
                }
            )
        expected_statuses = (
            {"declined"}
            if review.action == "decline"
            else {
                "approved",
                "endpoint_change_requested",
                "segment_change_requested",
                "applied",
                "closed",
            }
        )
        if any(
            decision.status not in expected_statuses
            or decision.reviewed_by != review.reviewed_by
            or decision.review_notes != review.review_notes
            for decision in decisions
        ):
            blockers.append(
                {
                    "code": "delegated_review_evidence_mismatch",
                    "message": "delegated decision review evidence differs from the batch attestation",
                }
            )
    elif any(decision.status != "proposed" for decision in decisions):
        blockers.append(
            {
                "code": "missing_batch_review_attestation",
                "message": "delegated decisions advanced without a batch review attestation",
            }
        )
    for run in runs:
        outcomes = run.result_payload.get("outcomes")
        if not isinstance(outcomes, list):
            blockers.append(
                {
                    "code": "run_outcomes_invalid",
                    "message": "stored run outcomes are not a list",
                    "run_id": str(run.id),
                }
            )
            continue
        outcome_counts = Counter(
            str(item.get("outcome")) for item in outcomes if isinstance(item, dict)
        )
        payload_matches_columns = bool(
            run.result_payload.get("batch_id") == str(batch.id)
            and run.result_payload.get("batch_manifest_sha256") == batch.manifest_sha256
            and run.result_payload.get("executed_by") == run.executed_by
            and run.result_payload.get("remaining_actionable_count")
            == run.remaining_actionable_count
            and run.result_payload.get("requested_limit") == run.requested_limit
            and run.result_payload.get("run_id") == str(run.id)
            and run.result_payload.get("run_type") == run.run_type
            and run.result_payload.get("schema_version") == 1
        )
        if (
            _digest(run.result_payload) != run.result_sha256
            or run.proposal_batch_id != batch.id
            or review is None
            or run.batch_review_id != review.id
            or run.batch_manifest_sha256 != batch.manifest_sha256
            or not payload_matches_columns
            or run.scanned_count != len(outcomes)
            or run.endpoint_pending_count != outcome_counts["endpoint_change_requested"]
            or run.segment_pending_count != outcome_counts["segment_change_requested"]
            or run.applied_count != outcome_counts["applied"]
            or run.closed_count != outcome_counts["closed"]
            or run.error_count != outcome_counts["error"]
        ):
            blockers.append(
                {
                    "code": "run_result_evidence_mismatch",
                    "message": "stored run evidence does not match its exact outcomes",
                    "run_id": str(run.id),
                }
            )
    return blockers


def _decision_run_evidence(
    decision: FiberTopologyConnectivityDecision,
    runs: list[FiberTopologyConnectivityRun],
    invalid_run_ids: set[str],
) -> tuple[str, list[dict[str, object]]]:
    if decision.status not in EXECUTED_DECISION_STATUSES:
        return "not_required", []
    evidence: list[dict[str, object]] = []
    for run in runs:
        outcomes = run.result_payload.get("outcomes")
        if not isinstance(outcomes, list):
            continue
        for outcome in outcomes:
            if not isinstance(outcome, dict) or outcome.get("decision_id") != str(
                decision.id
            ):
                continue
            evidence.append(
                {
                    "outcome": str(outcome.get("outcome")),
                    "result_sha256": run.result_sha256,
                    "run_id": str(run.id),
                    "run_type": run.run_type,
                }
            )
    if not evidence:
        return "missing", evidence
    latest = evidence[-1]
    if str(latest["run_id"]) in invalid_run_ids:
        return "invalid", evidence
    if latest["outcome"] != decision.status:
        return "stale", evidence
    return "current", evidence


def _resolution_evidence(resolution) -> dict[str, object] | None:
    if resolution is None:
        return None
    request = resolution.change_request
    return {
        "change_request_id": (
            str(resolution.change_request_id) if resolution.change_request_id else None
        ),
        "change_request_status": _enum_value(request.status) if request else None,
        "endpoint_ref_id": str(resolution.endpoint_ref_id),
        "endpoint_type": resolution.endpoint_type,
        "resolution_id": str(resolution.id),
        "resolution_status": resolution.status,
        "termination_point_id": (
            str(resolution.termination_point_id)
            if resolution.termination_point_id
            else None
        ),
    }


def _mutation_evidence(
    decision: FiberTopologyConnectivityDecision,
) -> dict[str, object]:
    segment_request = decision.segment_change_request
    return {
        "end_resolution": _resolution_evidence(decision.end_resolution),
        "segment_change_request_id": (
            str(decision.segment_change_request_id)
            if decision.segment_change_request_id
            else None
        ),
        "segment_change_request_status": (
            _enum_value(segment_request.status) if segment_request else None
        ),
        "start_resolution": _resolution_evidence(decision.start_resolution),
    }


def _provenance_evidence(
    decision: FiberTopologyConnectivityDecision,
    link: FiberTopologySegmentSourceLink | None,
    segment: FiberSegment | None,
    current_content_sha256: str,
) -> tuple[bool, dict[str, object]]:
    segment_valid = bool(
        segment
        and segment.is_active
        and segment.from_point_id
        and segment.to_point_id
        and segment.from_point_id != segment.to_point_id
        and segment.route_geom is not None
    )
    link_valid = bool(
        link
        and link.status == "active"
        and link.decision_id == decision.id
        and link.staged_feature_id == decision.staged_feature_id
        and link.source_system == decision.source_system
        and link.source_asset_type == decision.source_asset_type
        and link.external_id == decision.source_external_id
        and link.content_sha256 == current_content_sha256
        and link.segment_id == decision.canonical_segment_id
    )
    return segment_valid and link_valid, {
        "canonical_segment_id": (
            str(decision.canonical_segment_id)
            if decision.canonical_segment_id
            else None
        ),
        "segment_current": segment_valid,
        "source_link_content_sha256": link.content_sha256 if link else None,
        "source_link_id": str(link.id) if link else None,
        "source_link_status": link.status if link else None,
        "source_link_valid": link_valid,
    }


def _decision_summary(
    decision: FiberTopologyConnectivityDecision,
    *,
    batch: FiberTopologyConnectivityProposalBatch | None,
    review: FiberTopologyConnectivityBatchReview | None,
    batch_blockers: list[dict[str, object]],
    run_state: str,
    run_evidence: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "action": decision.action,
        "batch_evidence_state": (
            "missing" if batch is None else "invalid" if batch_blockers else "current"
        ),
        "batch_id": str(batch.id) if batch else None,
        "batch_manifest_sha256": batch.manifest_sha256 if batch else None,
        "closed_reason": decision.closed_reason,
        "decision_id": str(decision.id),
        "decision_sha256": decision.decision_sha256,
        "end_endpoint_ref_id": (
            str(decision.end_endpoint_ref_id) if decision.end_endpoint_ref_id else None
        ),
        "end_endpoint_type": decision.end_endpoint_type,
        "execution_evidence": run_evidence,
        "execution_evidence_state": run_state,
        "feature_content_sha256": decision.feature_content_sha256,
        "proposed_at": _timestamp(decision.proposed_at),
        "review_action": review.action if review else None,
        "review_attestation_sha256": review.attestation_sha256 if review else None,
        "status": decision.status,
        "start_endpoint_ref_id": (
            str(decision.start_endpoint_ref_id)
            if decision.start_endpoint_ref_id
            else None
        ),
        "start_endpoint_type": decision.start_endpoint_type,
    }


def _selected_decision(
    exact: list[FiberTopologyConnectivityDecision],
) -> tuple[FiberTopologyConnectivityDecision | None, bool]:
    applicable = [
        decision
        for decision in exact
        if decision.status in ACTIVE_DECISION_STATUSES
        or decision.status == "applied"
        or (
            decision.status == "closed"
            and decision.action == "reject"
            and decision.closed_reason == "source_path_rejected"
        )
    ]
    if len(applicable) > 1:
        return None, True
    if applicable:
        return applicable[0], False
    return (exact[0], False) if exact else (None, False)


def _lifecycle_state(
    decision: FiberTopologyConnectivityDecision,
    *,
    batch: FiberTopologyConnectivityProposalBatch | None,
    review: FiberTopologyConnectivityBatchReview | None,
    batch_blockers: list[dict[str, object]],
    execution_evidence_state: str,
    provenance_current: bool,
) -> str:
    if batch is None:
        return "missing_batch_evidence"
    if batch_blockers:
        return "batch_evidence_drift"
    if decision.status == "proposed":
        return "batch_evidence_drift" if review else "pending_review"
    expected_review = "decline" if decision.status == "declined" else "approve"
    if review is None or review.action != expected_review:
        return "batch_evidence_drift"
    if decision.status == "declined":
        return "declined"
    if decision.status == "approved":
        return "pending_execution"
    if execution_evidence_state != "current":
        return "execution_evidence_drift"
    if decision.status == "endpoint_change_requested":
        return "pending_endpoint_mutation"
    if decision.status == "segment_change_requested":
        return "pending_segment_mutation"
    if decision.status == "applied":
        return "applied_current" if provenance_current else "provenance_drift"
    if decision.action == "reject" and decision.closed_reason == "source_path_rejected":
        return "rejected_current"
    if decision.closed_reason in STALE_CLOSED_REASONS:
        return "stale_closed"
    if decision.closed_reason in CHANGE_REJECTED_REASONS:
        return "change_rejected"
    return "other_closed"


def reconcile_fiber_connectivity_coverage(
    db: Session,
) -> FiberTopologyConnectivityCoverageReport:
    """Reconcile every latest staged path to current reviewed lineage without writes."""

    ensure_connectivity_coverage_repeatable_snapshot(db)
    features = _latest_staged_paths(db)
    field_verification = project_field_verification_evidence(db, features)
    decisions = list(
        db.scalars(
            select(FiberTopologyConnectivityDecision)
            .options(
                joinedload(
                    FiberTopologyConnectivityDecision.start_resolution
                ).joinedload(FiberTopologyTerminationResolution.change_request),
                joinedload(FiberTopologyConnectivityDecision.end_resolution).joinedload(
                    FiberTopologyTerminationResolution.change_request
                ),
                joinedload(FiberTopologyConnectivityDecision.segment_change_request),
            )
            .order_by(
                FiberTopologyConnectivityDecision.proposed_at.desc(),
                FiberTopologyConnectivityDecision.id.desc(),
            )
        ).all()
    )
    batches = list(
        db.scalars(
            select(FiberTopologyConnectivityProposalBatch).order_by(
                FiberTopologyConnectivityProposalBatch.created_at,
                FiberTopologyConnectivityProposalBatch.id,
            )
        ).all()
    )
    reviews = list(db.scalars(select(FiberTopologyConnectivityBatchReview)).all())
    runs = list(
        db.scalars(
            select(FiberTopologyConnectivityRun).order_by(
                FiberTopologyConnectivityRun.executed_at,
                FiberTopologyConnectivityRun.id,
            )
        ).all()
    )
    links = list(db.scalars(select(FiberTopologySegmentSourceLink)).all())
    canonical_segment_ids = {
        decision.canonical_segment_id
        for decision in decisions
        if decision.canonical_segment_id
    }
    segments = (
        list(
            db.scalars(
                select(FiberSegment).where(FiberSegment.id.in_(canonical_segment_ids))
            ).all()
        )
        if canonical_segment_ids
        else []
    )
    segment_by_id = {str(segment.id): segment for segment in segments}

    decisions_by_source: dict[
        tuple[str, str, str], list[FiberTopologyConnectivityDecision]
    ] = defaultdict(list)
    decisions_by_batch: dict[str, list[FiberTopologyConnectivityDecision]] = (
        defaultdict(list)
    )
    for decision in decisions:
        decisions_by_source[_source_key_from_decision(decision)].append(decision)
        if decision.proposal_batch_id:
            decisions_by_batch[str(decision.proposal_batch_id)].append(decision)
    for rows in decisions_by_batch.values():
        rows.sort(key=lambda item: item.proposal_batch_row_number or 0)
    batch_by_id = {str(batch.id): batch for batch in batches}
    review_by_batch = {str(review.proposal_batch_id): review for review in reviews}
    runs_by_batch: dict[str, list[FiberTopologyConnectivityRun]] = defaultdict(list)
    for run in runs:
        runs_by_batch[str(run.proposal_batch_id)].append(run)
    link_by_decision = {str(link.decision_id): link for link in links}

    blockers: list[dict[str, object]] = []
    blockers_by_batch: dict[str, list[dict[str, object]]] = {}
    invalid_run_ids_by_batch: dict[str, set[str]] = defaultdict(set)
    for proposal_batch in batches:
        batch_id = str(proposal_batch.id)
        batch_blockers = _batch_blockers(
            proposal_batch,
            decisions_by_batch.get(batch_id, []),
            review_by_batch.get(batch_id),
            runs_by_batch.get(batch_id, []),
        )
        blockers_by_batch[batch_id] = batch_blockers
        for blocker in batch_blockers:
            evidence = {"batch_id": batch_id, **blocker}
            blockers.append(evidence)
            if blocker["code"] in RUN_BLOCKER_CODES:
                invalid_run_ids_by_batch[batch_id].add(str(blocker.get("run_id")))

    lineage_rows: list[dict[str, object]] = []
    lineage_by_decision: dict[str, dict[str, object]] = {}
    for decision in decisions:
        batch_id = str(decision.proposal_batch_id) if decision.proposal_batch_id else ""
        decision_batch = batch_by_id.get(batch_id)
        review = review_by_batch.get(batch_id)
        batch_runs = runs_by_batch.get(batch_id, [])
        integrity_blockers = [
            blocker
            for blocker in blockers_by_batch.get(batch_id, [])
            if blocker["code"] not in RUN_BLOCKER_CODES
        ]
        run_state, run_evidence = _decision_run_evidence(
            decision,
            batch_runs,
            invalid_run_ids_by_batch.get(batch_id, set()),
        )
        row = _decision_summary(
            decision,
            batch=decision_batch,
            review=review,
            batch_blockers=integrity_blockers,
            run_state=run_state,
            run_evidence=run_evidence,
        )
        lineage_rows.append(row)
        lineage_by_decision[str(decision.id)] = row

    coverage_states: list[str] = []
    lifecycle_states: list[str] = []
    selected_decision_statuses: list[str] = []
    path_rows: list[dict[str, object]] = []
    source_batch_ids: set[str] = set()
    for feature in features:
        source_batch_ids.add(str(feature.batch_id))
        source_key = _source_key_from_feature(feature)
        candidates = decisions_by_source.get(source_key, [])
        exact = [
            decision
            for decision in candidates
            if decision.feature_content_sha256 == feature.content_sha256
        ]
        selected, overlapping = _selected_decision(exact)
        provenance: dict[str, object] | None = None
        source_blocked = bool(
            feature.external_id is None
            or feature.match_status == "blocked"
            or feature.geometry_type != "LineString"
            or not isinstance(feature.geometry_geojson.get("coordinates"), list)
            or len(feature.geometry_geojson.get("coordinates") or []) < 2
        )
        if source_blocked:
            coverage_state = "source_blocked"
            lifecycle_state = "source_identity_blocked"
        elif overlapping:
            coverage_state = "ambiguous_overlapping_coverage"
            lifecycle_state = "overlapping_decision_evidence"
        elif selected:
            coverage_state = "exact"
            batch_id = (
                str(selected.proposal_batch_id) if selected.proposal_batch_id else ""
            )
            link = link_by_decision.get(str(selected.id))
            segment = segment_by_id.get(str(selected.canonical_segment_id))
            provenance_current, provenance = _provenance_evidence(
                selected, link, segment, feature.content_sha256
            )
            lifecycle_state = _lifecycle_state(
                selected,
                batch=batch_by_id.get(batch_id),
                review=review_by_batch.get(batch_id),
                batch_blockers=[
                    blocker
                    for blocker in blockers_by_batch.get(batch_id, [])
                    if blocker["code"] not in RUN_BLOCKER_CODES
                ],
                execution_evidence_state=str(
                    lineage_by_decision[str(selected.id)]["execution_evidence_state"]
                ),
                provenance_current=provenance_current,
            )
            selected_decision_statuses.append(selected.status)
        elif candidates:
            coverage_state = "superseded_evidence"
            lifecycle_state = "stale_source_evidence"
        else:
            coverage_state = "unassigned"
            lifecycle_state = "missing_endpoint_evidence"
        coverage_states.append(coverage_state)
        lifecycle_states.append(lifecycle_state)
        selected_id = str(selected.id) if selected else None
        path_rows.append(
            {
                "batch_id": str(feature.batch_id),
                "content_sha256": feature.content_sha256,
                "coverage_state": coverage_state,
                "display_name": feature.display_name,
                "exact_decision_ids": [str(decision.id) for decision in exact],
                "external_id": feature.external_id,
                "field_verification": field_verification[str(feature.id)],
                "geometry_sha256": feature.geometry_sha256,
                "lifecycle_state": lifecycle_state,
                "match_status": feature.match_status,
                "mutation_evidence": (
                    _mutation_evidence(selected) if selected else None
                ),
                "profile": feature.batch.profile,
                "provenance_evidence": provenance if selected else None,
                "selected_decision": (
                    lineage_by_decision[selected_id] if selected_id else None
                ),
                "source_system": feature.batch.source_system,
                "staged_feature_id": str(feature.id),
                "superseded_decision_ids": [
                    str(decision.id) for decision in candidates if decision not in exact
                ],
            }
        )

    coverage_counts = _counts(coverage_states, COVERAGE_STATES)
    lifecycle_counts = _counts(lifecycle_states, LIFECYCLE_STATES)
    decision_status_names = (
        "proposed",
        "approved",
        "declined",
        "endpoint_change_requested",
        "segment_change_requested",
        "applied",
        "closed",
    )
    decision_counts = _counts(selected_decision_statuses, decision_status_names)
    field_verification_counts = field_verification_state_counts(field_verification)
    cohort_nonempty = bool(features)
    source_reviewable = coverage_counts["source_blocked"] == 0
    exact_once = coverage_counts["exact"] == len(features)
    evidence_current = all(
        lifecycle_counts[state] == 0
        for state in (
            "missing_batch_evidence",
            "batch_evidence_drift",
            "execution_evidence_drift",
        )
    )
    no_pending_review = lifecycle_counts["pending_review"] == 0
    no_pending_execution = lifecycle_counts["pending_execution"] == 0
    no_pending_mutation = (
        lifecycle_counts["pending_endpoint_mutation"] == 0
        and lifecycle_counts["pending_segment_mutation"] == 0
    )
    terminal_current = all(
        state in {"applied_current", "rejected_current"} for state in lifecycle_states
    ) and bool(lifecycle_states)
    gates = (
        _gate(
            "complete_latest_staged_path_cohort_nonempty",
            cohort_nonempty,
            "The report scanned every latest staged fiber_segment source identity and the cohort is non-empty.",
        ),
        _gate(
            "latest_staged_path_sources_reviewable",
            source_reviewable,
            "No latest path has blocked identity or incomplete LineString evidence.",
        ),
        _gate(
            "latest_paths_exactly_covered_once",
            exact_once,
            "Every latest path has exactly one applicable decision for its exact current content.",
        ),
        _gate(
            "batch_review_and_run_evidence_current",
            evidence_current and not blockers,
            "Every selected decision has intact Phase 16 manifest, review, and required run evidence.",
        ),
        _gate(
            "no_pending_connectivity_review",
            no_pending_review,
            "No selected path decision remains proposed.",
        ),
        _gate(
            "no_pending_connectivity_execution",
            no_pending_execution,
            "No selected path decision remains approved but unexecuted.",
        ),
        _gate(
            "no_pending_canonical_fiber_mutation",
            no_pending_mutation,
            "No selected path waits for termination or segment change-request reconciliation.",
        ),
        _gate(
            "all_paths_terminally_resolved_current",
            terminal_current,
            "Every latest path is either applied with current canonical provenance or explicitly reviewed and rejected.",
        ),
    )
    ready = all(bool(gate["ready"]) for gate in gates)
    report_payload: dict[str, object] = {
        "batch_evidence_blockers": blockers,
        "coverage_counts": coverage_counts,
        "decision_counts": decision_counts,
        "field_verification_counts": field_verification_counts,
        "gates": list(gates),
        "lifecycle_counts": lifecycle_counts,
        "lineages": lineage_rows,
        "paths": path_rows,
        "ready_for_connectivity_cutover_review": ready,
        "schema_version": 1,
        "source_batch_count": len(source_batch_ids),
        "staged_path_count": len(features),
    }
    return FiberTopologyConnectivityCoverageReport(
        coverage_report_sha256=_digest(report_payload),
        staged_path_count=len(features),
        source_batch_count=len(source_batch_ids),
        paths=tuple(path_rows),
        lineages=tuple(lineage_rows),
        coverage_counts=coverage_counts,
        lifecycle_counts=lifecycle_counts,
        decision_counts=decision_counts,
        field_verification_counts=field_verification_counts,
        batch_evidence_blockers=tuple(blockers),
        gates=gates,
        ready_for_connectivity_cutover_review=ready,
    )


__all__ = [
    "COVERAGE_STATES",
    "LIFECYCLE_STATES",
    "FiberTopologyConnectivityCoverageError",
    "FiberTopologyConnectivityCoverageReport",
    "ensure_connectivity_coverage_repeatable_snapshot",
    "reconcile_fiber_connectivity_coverage",
]
