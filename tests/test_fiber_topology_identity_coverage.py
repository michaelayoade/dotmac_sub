from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.fiber_change_request import FiberChangeRequest, FiberChangeRequestStatus
from app.models.fiber_topology_identity import (
    FiberTopologyAssetSourceLink,
    FiberTopologyIdentityDecision,
    FiberTopologyIdentityExecutionRun,
    FiberTopologyIdentityProposalBatch,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import FdhCabinet
from app.services import fiber_change_requests
from app.services.network.fiber_topology_identity import propose_identity_decision
from app.services.network.fiber_topology_identity_coverage import (
    reconcile_fiber_identity_coverage,
)
from app.services.network.fiber_topology_review import (
    attest_identity_batch,
    execute_identity_batch,
    propose_identity_batch,
)
from app.web.admin import network_fiber_plant


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage_point(
    db_session,
    asset_type: str,
    external_id: str,
    *,
    created_at: datetime | None = None,
    match_status: str = "new",
) -> FiberTopologyStagedFeature:
    observed_at = created_at or datetime.now(UTC)
    blocked = match_status == "blocked"
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_identity_coverage_{uuid.uuid4().hex}",
        source_name="pytest-point-identity-coverage.kmz",
        asset_type=asset_type,
        external_id_key="source_id",
        file_sha256=_sha(),
        manifest_sha256=_sha(),
        status="blocked" if blocked else "staged",
        feature_count=1,
        blocker_count=1 if blocked else 0,
        candidate_count=0,
        unchanged_count=0,
        new_count=0 if blocked else 1,
        source_metadata={"test": True},
        created_by="pytest-stager",
        created_at=observed_at,
    )
    feature = FiberTopologyStagedFeature(
        batch=batch,
        row_number=1,
        asset_type=asset_type,
        external_id=external_id,
        display_name=external_id,
        geometry_type="Point",
        geometry_geojson={"type": "Point", "coordinates": [7.40, 9.00]},
        source_properties={"Placement": "aerial", "Type": "FAT"},
        content_sha256=_sha(),
        geometry_sha256=_sha(),
        match_status=match_status,
        blocker_codes=["pytest_blocker"] if blocked else [],
        match_reasons=[],
        candidate_asset_ids=[],
        created_at=observed_at,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def _approve(db_session, proposal):
    return attest_identity_batch(
        db_session,
        proposal.batch_id,
        expected_manifest_sha256=proposal.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Exact source identity evidence independently checked",
    )


def _execute(db_session, proposal):
    return execute_identity_batch(
        db_session,
        proposal.batch_id,
        expected_manifest_sha256=proposal.manifest_sha256,
        executed_by="executor@example.com",
        limit=100,
    )


def test_empty_and_unassigned_reports_are_stable_and_read_only(db_session):
    empty = reconcile_fiber_identity_coverage(db_session)
    assert empty.staged_point_count == 0
    assert empty.ready_for_point_identity_cutover_review is False

    point = _stage_point(db_session, "fiber_access_point", "FAT-COVERAGE-EMPTY")
    before = (
        db_session.query(FiberTopologyIdentityProposalBatch).count(),
        db_session.query(FiberTopologyIdentityDecision).count(),
        db_session.query(FiberTopologyIdentityExecutionRun).count(),
    )
    report = reconcile_fiber_identity_coverage(db_session)
    replay = reconcile_fiber_identity_coverage(db_session)

    assert report.assets[0]["staged_feature_id"] == str(point.id)
    assert report.assets[0]["coverage_state"] == "unassigned"
    assert report.assets[0]["lifecycle_state"] == "missing_identity_decision"
    assert replay.coverage_report_sha256 == report.coverage_report_sha256
    assert before == (
        db_session.query(FiberTopologyIdentityProposalBatch).count(),
        db_session.query(FiberTopologyIdentityDecision).count(),
        db_session.query(FiberTopologyIdentityExecutionRun).count(),
    )


def test_supported_link_and_support_rejection_can_satisfy_all_evidence_gates(
    db_session,
):
    cabinet_feature = _stage_point(db_session, "fdh_cabinet", "CAB-COVERAGE-READY")
    support_feature = _stage_point(
        db_session, "support_structure", "POLE-COVERAGE-REJECT"
    )
    cabinet = FdhCabinet(name="Coverage cabinet", code="CAB-COVERAGE-READY")
    db_session.add(cabinet)
    db_session.commit()
    proposal = propose_identity_batch(
        db_session,
        [
            {
                "action": "link_existing",
                "staged_feature_id": str(cabinet_feature.id),
                "target_asset_id": str(cabinet.id),
            },
            {"action": "reject", "staged_feature_id": str(support_feature.id)},
        ],
        proposed_by="planner@example.com",
        reason="Field-verified point identity cohort",
    )
    _approve(db_session, proposal)
    _execute(db_session, proposal)

    report = reconcile_fiber_identity_coverage(db_session)
    rows = {row["external_id"]: row for row in report.assets}

    assert rows["CAB-COVERAGE-READY"]["lifecycle_state"] == "applied_current"
    assert rows["POLE-COVERAGE-REJECT"]["canonical_model_state"] == (
        "unsupported_reject_only"
    )
    assert rows["POLE-COVERAGE-REJECT"]["lifecycle_state"] == "rejected_current"
    assert report.ready_for_point_identity_cutover_review is True
    assert all(gate["ready"] is True for gate in report.gates)


def test_batch_lifecycle_keeps_review_and_execution_pending_states_distinct(
    db_session,
):
    point = _stage_point(db_session, "fiber_access_point", "FAT-COVERAGE-PENDING")
    proposal = propose_identity_batch(
        db_session,
        [{"action": "reject", "staged_feature_id": str(point.id)}],
        proposed_by="planner@example.com",
        reason="Review staged FAT identity",
    )

    proposed = reconcile_fiber_identity_coverage(db_session)
    assert proposed.assets[0]["lifecycle_state"] == "pending_review"

    _approve(db_session, proposal)
    approved = reconcile_fiber_identity_coverage(db_session)
    assert approved.assets[0]["lifecycle_state"] == "pending_execution"

    _execute(db_session, proposal)
    rejected = reconcile_fiber_identity_coverage(db_session)
    assert rejected.assets[0]["lifecycle_state"] == "rejected_current"
    assert rejected.ready_for_point_identity_cutover_review is True


def test_create_request_and_result_reconciliation_remain_separate(
    db_session, subscriber, monkeypatch
):
    point = _stage_point(db_session, "fiber_access_point", "FAT-COVERAGE-CREATE")
    proposal = propose_identity_batch(
        db_session,
        [{"action": "create", "staged_feature_id": str(point.id)}],
        proposed_by="planner@example.com",
        reason="Create field-verified FAT",
    )
    _approve(db_session, proposal)
    _execute(db_session, proposal)

    pending = reconcile_fiber_identity_coverage(db_session)
    assert pending.assets[0]["lifecycle_state"] == "pending_canonical_mutation"
    request = db_session.query(FiberChangeRequest).one()
    assert request.status == FiberChangeRequestStatus.pending

    monkeypatch.setattr(fiber_change_requests, "_geojson_to_geom", lambda _value: None)
    fiber_change_requests.approve_request(
        db_session,
        str(request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical FAT creation independently approved",
    )
    awaiting_projection = reconcile_fiber_identity_coverage(db_session)
    assert awaiting_projection.assets[0]["lifecycle_state"] == (
        "pending_result_reconciliation"
    )

    from app.services.network.fiber_topology_identity import finalize_identity_decision

    finalize_identity_decision(
        db_session,
        proposal.decision_ids[0],
        finalized_by="reconciler@example.com",
    )
    applied = reconcile_fiber_identity_coverage(db_session)
    assert applied.assets[0]["lifecycle_state"] == "applied_current"
    assert applied.assets[0]["provenance_evidence"]["source_link_valid"] is True
    assert applied.ready_for_point_identity_cutover_review is True


def test_new_source_content_supersedes_old_exact_identity_decision(db_session):
    observed_at = datetime.now(UTC)
    first = _stage_point(
        db_session,
        "fdh_cabinet",
        "CAB-COVERAGE-DRIFT",
        created_at=observed_at,
    )
    propose_identity_batch(
        db_session,
        [{"action": "reject", "staged_feature_id": str(first.id)}],
        proposed_by="planner@example.com",
        reason="Review initial cabinet observation",
    )
    latest = _stage_point(
        db_session,
        "fdh_cabinet",
        "CAB-COVERAGE-DRIFT",
        created_at=observed_at + timedelta(seconds=1),
    )

    report = reconcile_fiber_identity_coverage(db_session)

    assert report.staged_point_count == 1
    assert report.assets[0]["staged_feature_id"] == str(latest.id)
    assert report.assets[0]["coverage_state"] == "superseded_evidence"
    assert report.assets[0]["lifecycle_state"] == "stale_source_evidence"


def test_standalone_identity_decision_lacks_batch_control_evidence(db_session):
    point = _stage_point(db_session, "splice_closure", "SPLICE-COVERAGE-STANDALONE")
    propose_identity_decision(
        db_session,
        point.id,
        "reject",
        proposed_by="planner@example.com",
        reason="Compatibility decision without batch control",
    )

    report = reconcile_fiber_identity_coverage(db_session)

    assert report.assets[0]["coverage_state"] == "exact"
    assert report.assets[0]["lifecycle_state"] == "missing_batch_evidence"


def test_applied_link_requires_current_canonical_source_provenance(db_session):
    point = _stage_point(db_session, "fdh_cabinet", "CAB-COVERAGE-PROVENANCE")
    cabinet = FdhCabinet(name="Provenance cabinet", code="CAB-COVERAGE-PROVENANCE")
    db_session.add(cabinet)
    db_session.commit()
    proposal = propose_identity_batch(
        db_session,
        [
            {
                "action": "link_existing",
                "staged_feature_id": str(point.id),
                "target_asset_id": str(cabinet.id),
            }
        ],
        proposed_by="planner@example.com",
        reason="Bind exact cabinet source identity",
    )
    _approve(db_session, proposal)
    _execute(db_session, proposal)

    current = reconcile_fiber_identity_coverage(db_session)
    assert current.assets[0]["lifecycle_state"] == "applied_current"

    link = db_session.query(FiberTopologyAssetSourceLink).one()
    link.content_sha256 = _sha()
    db_session.commit()
    drift = reconcile_fiber_identity_coverage(db_session)
    assert drift.assets[0]["lifecycle_state"] == "provenance_drift"
    assert drift.ready_for_point_identity_cutover_review is False


def test_tampered_execution_run_is_evidence_drift(db_session):
    point = _stage_point(db_session, "fiber_access_point", "FAT-COVERAGE-RUN-DRIFT")
    proposal = propose_identity_batch(
        db_session,
        [{"action": "reject", "staged_feature_id": str(point.id)}],
        proposed_by="planner@example.com",
        reason="Review exact FAT source identity",
    )
    _approve(db_session, proposal)
    _execute(db_session, proposal)
    run = db_session.query(FiberTopologyIdentityExecutionRun).one()
    run.result_payload = {**run.result_payload, "tampered": True}
    db_session.commit()

    report = reconcile_fiber_identity_coverage(db_session)

    assert report.assets[0]["lifecycle_state"] == "execution_evidence_drift"
    assert report.batch_evidence_blockers[0]["code"] == ("run_result_evidence_mismatch")


def test_failed_execution_remains_distinct_from_never_executed(db_session):
    point = _stage_point(db_session, "fdh_cabinet", "CAB-COVERAGE-EXEC-ERROR")
    cabinet = FdhCabinet(name="Execution target", code="CAB-COVERAGE-EXEC-ERROR")
    db_session.add(cabinet)
    db_session.commit()
    proposal = propose_identity_batch(
        db_session,
        [
            {
                "action": "link_existing",
                "staged_feature_id": str(point.id),
                "target_asset_id": str(cabinet.id),
            }
        ],
        proposed_by="planner@example.com",
        reason="Bind exact active cabinet target",
    )
    _approve(db_session, proposal)
    cabinet.is_active = False
    db_session.commit()

    result = _execute(db_session, proposal)
    report = reconcile_fiber_identity_coverage(db_session)

    assert result.counts["error"] == 1
    assert report.assets[0]["lifecycle_state"] == "execution_failed"
    assert report.assets[0]["selected_decision"]["execution_evidence_state"] == (
        "failed"
    )
    assert report.ready_for_point_identity_cutover_review is False


def test_admin_route_and_template_are_read_only_complete_cohort_projection():
    routes = {route.path: route for route in network_fiber_plant.router.routes}
    route = routes["/network/fiber-identity-coverage"]
    template = Path("templates/admin/network/fiber/identity_coverage.html")

    assert route.methods == {"GET"}
    assert template.exists()
    content = template.read_text()
    assert "gates always use the complete cohort" in content
    assert "cannot infer identities" in content
    assert "reject-only" in content
    assert "<form" not in content
