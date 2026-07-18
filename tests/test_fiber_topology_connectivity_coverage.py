from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.fiber_change_request import FiberChangeRequest, FiberChangeRequestStatus
from app.models.fiber_topology_connectivity import FiberTopologyConnectivityDecision
from app.models.fiber_topology_connectivity_review import (
    FiberTopologyConnectivityProposalBatch,
    FiberTopologyConnectivityRun,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
    FiberTerminationPoint,
    ODNEndpointType,
)
from app.services.network.fiber_topology_connectivity import (
    propose_connectivity_decision,
)
from app.services.network.fiber_topology_connectivity_coverage import (
    reconcile_fiber_connectivity_coverage,
)
from app.services.network.fiber_topology_connectivity_review import (
    attest_connectivity_batch,
    execute_connectivity_batch,
    propose_connectivity_batch,
)
from app.web.admin import network_fiber_plant


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage_path(
    db_session,
    external_id: str,
    *,
    created_at: datetime | None = None,
) -> FiberTopologyStagedFeature:
    observed_at = created_at or datetime.now(UTC)
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_coverage_{uuid.uuid4().hex}",
        source_name="pytest-connectivity-coverage.kmz",
        asset_type="fiber_segment",
        external_id_key="spanid",
        file_sha256=_sha(),
        manifest_sha256=_sha(),
        status="staged",
        feature_count=1,
        blocker_count=0,
        candidate_count=0,
        unchanged_count=0,
        new_count=1,
        source_metadata={"test": True},
        created_by="pytest-stager",
        created_at=observed_at,
    )
    feature = FiberTopologyStagedFeature(
        batch=batch,
        row_number=1,
        asset_type="fiber_segment",
        external_id=external_id,
        display_name=external_id,
        geometry_type="LineString",
        geometry_geojson={
            "type": "LineString",
            "coordinates": [[7.40, 9.00], [7.42, 9.02]],
        },
        source_properties={"span_type": "distribution"},
        content_sha256=_sha(),
        geometry_sha256=_sha(),
        match_status="new",
        blocker_codes=[],
        match_reasons=[],
        candidate_asset_ids=[],
        created_at=observed_at,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def _reject_item(path) -> dict:
    return {
        "action": "reject",
        "expected_feature_content_sha256": path.content_sha256,
        "staged_feature_id": str(path.id),
        "reason": "Verified duplicate drawing path",
    }


def _propose_reject_batch(db_session, path):
    return propose_connectivity_batch(
        db_session,
        [_reject_item(path)],
        proposed_by="planner@example.com",
        reason="Review the staged path cohort",
        source_name="coverage-test",
    )


def _approve(db_session, proposal):
    return attest_connectivity_batch(
        db_session,
        proposal.batch_id,
        expected_manifest_sha256=proposal.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Exact source and endpoint evidence checked",
    )


def test_empty_and_unassigned_reports_are_stable_and_read_only(db_session):
    empty = reconcile_fiber_connectivity_coverage(db_session)
    assert empty.staged_path_count == 0
    assert empty.ready_for_connectivity_cutover_review is False

    path = _stage_path(db_session, "SPAN-COVERAGE-UNASSIGNED")
    before = (
        db_session.query(FiberTopologyConnectivityProposalBatch).count(),
        db_session.query(FiberTopologyConnectivityDecision).count(),
        db_session.query(FiberTopologyConnectivityRun).count(),
    )
    report = reconcile_fiber_connectivity_coverage(db_session)
    replay = reconcile_fiber_connectivity_coverage(db_session)

    assert report.staged_path_count == 1
    assert report.paths[0]["staged_feature_id"] == str(path.id)
    assert report.paths[0]["coverage_state"] == "unassigned"
    assert report.paths[0]["lifecycle_state"] == "missing_endpoint_evidence"
    assert replay.coverage_report_sha256 == report.coverage_report_sha256
    assert before == (
        db_session.query(FiberTopologyConnectivityProposalBatch).count(),
        db_session.query(FiberTopologyConnectivityDecision).count(),
        db_session.query(FiberTopologyConnectivityRun).count(),
    )


def test_exact_batch_lifecycle_keeps_review_execution_and_rejection_distinct(
    db_session,
):
    path = _stage_path(db_session, "SPAN-COVERAGE-REJECT")
    proposal = _propose_reject_batch(db_session, path)

    proposed = reconcile_fiber_connectivity_coverage(db_session)
    assert proposed.paths[0]["coverage_state"] == "exact"
    assert proposed.paths[0]["lifecycle_state"] == "pending_review"

    _approve(db_session, proposal)
    approved = reconcile_fiber_connectivity_coverage(db_session)
    assert approved.paths[0]["lifecycle_state"] == "pending_execution"

    execute_connectivity_batch(
        db_session,
        proposal.batch_id,
        expected_manifest_sha256=proposal.manifest_sha256,
        executed_by="executor@example.com",
        limit=1,
    )
    rejected = reconcile_fiber_connectivity_coverage(db_session)

    assert rejected.paths[0]["lifecycle_state"] == "rejected_current"
    assert rejected.lifecycle_counts["rejected_current"] == 1
    assert rejected.ready_for_connectivity_cutover_review is True
    assert all(gate["ready"] is True for gate in rejected.gates)


def test_create_path_waiting_for_terminations_is_not_cutover_ready(db_session):
    path = _stage_path(db_session, "SPAN-COVERAGE-PENDING")
    cabinet = FdhCabinet(name="Coverage cabinet", code="COVERAGE-CAB")
    access_point = FiberAccessPoint(name="Coverage FAT", code="COVERAGE-FAT")
    db_session.add_all([cabinet, access_point])
    db_session.commit()
    proposal = propose_connectivity_batch(
        db_session,
        [
            {
                "action": "create",
                "expected_feature_content_sha256": path.content_sha256,
                "staged_feature_id": str(path.id),
                "start_endpoint_type": "fdh",
                "start_endpoint_ref_id": str(cabinet.id),
                "end_endpoint_type": "fiber_access_point",
                "end_endpoint_ref_id": str(access_point.id),
                "reason": "Field-verified path endpoints",
            }
        ],
        proposed_by="planner@example.com",
        reason="Create reviewed canonical path",
    )
    _approve(db_session, proposal)
    execute_connectivity_batch(
        db_session,
        proposal.batch_id,
        expected_manifest_sha256=proposal.manifest_sha256,
        executed_by="executor@example.com",
        limit=1,
    )

    report = reconcile_fiber_connectivity_coverage(db_session)

    assert report.paths[0]["lifecycle_state"] == "pending_endpoint_mutation"
    mutation = report.paths[0]["mutation_evidence"]
    assert mutation["start_resolution"]["change_request_status"] == "pending"
    assert mutation["end_resolution"]["change_request_status"] == "pending"
    assert db_session.query(FiberChangeRequest).count() == 2
    assert {
        request.status for request in db_session.query(FiberChangeRequest).all()
    } == {FiberChangeRequestStatus.pending}
    assert report.ready_for_connectivity_cutover_review is False


def test_new_source_content_supersedes_old_exact_decision(db_session):
    observed_at = datetime.now(UTC)
    first = _stage_path(db_session, "SPAN-COVERAGE-DRIFT", created_at=observed_at)
    _propose_reject_batch(db_session, first)
    latest = _stage_path(
        db_session,
        "SPAN-COVERAGE-DRIFT",
        created_at=observed_at + timedelta(seconds=1),
    )

    report = reconcile_fiber_connectivity_coverage(db_session)

    assert report.staged_path_count == 1
    assert report.paths[0]["staged_feature_id"] == str(latest.id)
    assert report.paths[0]["coverage_state"] == "superseded_evidence"
    assert report.paths[0]["lifecycle_state"] == "stale_source_evidence"
    assert report.ready_for_connectivity_cutover_review is False


def test_standalone_decision_is_exact_but_missing_phase16_batch_evidence(db_session):
    path = _stage_path(db_session, "SPAN-COVERAGE-STANDALONE")
    propose_connectivity_decision(
        db_session,
        path.id,
        "reject",
        proposed_by="planner@example.com",
        reason="Single-decision compatibility path",
    )

    report = reconcile_fiber_connectivity_coverage(db_session)

    assert report.paths[0]["coverage_state"] == "exact"
    assert report.paths[0]["lifecycle_state"] == "missing_batch_evidence"
    assert report.ready_for_connectivity_cutover_review is False


def test_link_existing_requires_current_segment_source_provenance(db_session):
    path = _stage_path(db_session, "SPAN-COVERAGE-APPLIED")
    cabinet = FdhCabinet(name="Applied cabinet", code="APPLIED-CAB")
    closure = FiberSpliceClosure(name="Applied closure")
    db_session.add_all([cabinet, closure])
    db_session.commit()
    start = FiberTerminationPoint(
        name="Applied start",
        endpoint_type=ODNEndpointType.fdh,
        ref_id=cabinet.id,
    )
    end = FiberTerminationPoint(
        name="Applied end",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
    )
    segment = FiberSegment(
        name="Applied existing segment",
        from_point=start,
        to_point=end,
        route_geom="LINESTRING(7.40 9.00, 7.42 9.02)",
    )
    db_session.add(segment)
    db_session.commit()
    proposal = propose_connectivity_batch(
        db_session,
        [
            {
                "action": "link_existing",
                "expected_feature_content_sha256": path.content_sha256,
                "staged_feature_id": str(path.id),
                "start_endpoint_type": "fdh",
                "start_endpoint_ref_id": str(cabinet.id),
                "end_endpoint_type": "splice_closure",
                "end_endpoint_ref_id": str(closure.id),
                "target_segment_id": str(segment.id),
                "reason": "Existing segment matches installed path",
            }
        ],
        proposed_by="planner@example.com",
        reason="Bind staged path to reviewed installed segment",
    )
    _approve(db_session, proposal)
    execute_connectivity_batch(
        db_session,
        proposal.batch_id,
        expected_manifest_sha256=proposal.manifest_sha256,
        executed_by="executor@example.com",
        limit=1,
    )

    current = reconcile_fiber_connectivity_coverage(db_session)
    assert current.paths[0]["lifecycle_state"] == "applied_current"
    assert current.paths[0]["provenance_evidence"]["source_link_valid"] is True
    assert current.ready_for_connectivity_cutover_review is True

    decision = db_session.get(
        FiberTopologyConnectivityDecision, proposal.decision_ids[0]
    )
    decision.source_link.content_sha256 = _sha()
    db_session.commit()
    drift = reconcile_fiber_connectivity_coverage(db_session)
    assert drift.paths[0]["lifecycle_state"] == "provenance_drift"
    assert drift.ready_for_connectivity_cutover_review is False


def test_tampered_run_evidence_is_execution_drift(db_session):
    path = _stage_path(db_session, "SPAN-COVERAGE-RUN-DRIFT")
    proposal = _propose_reject_batch(db_session, path)
    _approve(db_session, proposal)
    execute_connectivity_batch(
        db_session,
        proposal.batch_id,
        expected_manifest_sha256=proposal.manifest_sha256,
        executed_by="executor@example.com",
        limit=1,
    )
    run = db_session.query(FiberTopologyConnectivityRun).one()
    run.result_payload = {**run.result_payload, "tampered": True}
    db_session.commit()

    report = reconcile_fiber_connectivity_coverage(db_session)

    assert report.paths[0]["lifecycle_state"] == "execution_evidence_drift"
    assert report.batch_evidence_blockers[0]["code"] == ("run_result_evidence_mismatch")
    assert report.ready_for_connectivity_cutover_review is False


def test_admin_route_and_template_are_read_only_complete_cohort_projection():
    paths = {route.path: route for route in network_fiber_plant.router.routes}
    route = paths["/network/fiber-connectivity-coverage"]
    template = Path("templates/admin/network/fiber/connectivity_coverage.html")

    assert route.methods == {"GET"}
    assert template.exists()
    content = template.read_text()
    assert "gates always use the complete cohort" in content
    assert "cannot infer endpoints" in content
    assert "<form" not in content
