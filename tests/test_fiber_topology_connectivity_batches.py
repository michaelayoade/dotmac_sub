from __future__ import annotations

import uuid

import pytest

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestStatus,
)
from app.models.fiber_topology_connectivity import FiberTopologyConnectivityDecision
from app.models.fiber_topology_connectivity_review import (
    FiberTopologyConnectivityBatchReview,
    FiberTopologyConnectivityProposalBatch,
    FiberTopologyConnectivityRun,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import FdhCabinet, FiberAccessPoint
from app.services.network.fiber_topology_connectivity import (
    FiberTopologyConnectivityError,
)
from app.services.network.fiber_topology_connectivity_review import (
    FiberTopologyConnectivityProposalBatchBlocked,
    FiberTopologyConnectivityReviewError,
    attest_connectivity_batch,
    execute_connectivity_batch,
    inspect_connectivity_batch,
    preview_connectivity_proposal_batch,
    propose_connectivity_batch,
    reconcile_connectivity_batch,
)


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage_path(db_session, external_id: str) -> FiberTopologyStagedFeature:
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_connectivity_batch_{uuid.uuid4().hex}",
        source_name="pytest-connectivity-batch.kmz",
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
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def _create_item(path, cabinet, access_point) -> dict:
    return {
        "action": "create",
        "expected_feature_content_sha256": path.content_sha256,
        "staged_feature_id": str(path.id),
        "start_endpoint_type": "fdh",
        "start_endpoint_ref_id": str(cabinet.id),
        "end_endpoint_type": "fiber_access_point",
        "end_endpoint_ref_id": str(access_point.id),
        "fiber_count": 12,
        "segment_type": "distribution",
        "reason": "Field-verified endpoints",
    }


def _reject_item(path) -> dict:
    return {
        "action": "reject",
        "expected_feature_content_sha256": path.content_sha256,
        "staged_feature_id": str(path.id),
        "reason": "Duplicate drawing path",
    }


def test_preview_requires_exact_content_and_explicit_endpoints(db_session):
    path = _stage_path(db_session, "SPAN-BATCH-PREVIEW")
    preview = preview_connectivity_proposal_batch(
        db_session,
        [
            {
                "action": "create",
                "expected_feature_content_sha256": path.content_sha256,
                "staged_feature_id": str(path.id),
            }
        ],
        proposed_by="planner@example.com",
        reason="Review exact path endpoints",
    )

    assert preview.ready is False
    assert preview.blockers[0]["code"] == "connectivity_decision_blocked"
    assert "explicit endpoint IDs" in preview.blockers[0]["message"]
    assert db_session.query(FiberTopologyConnectivityProposalBatch).count() == 0
    assert db_session.query(FiberTopologyConnectivityDecision).count() == 0

    mismatch = preview_connectivity_proposal_batch(
        db_session,
        [
            {
                "action": "reject",
                "expected_feature_content_sha256": _sha(),
                "staged_feature_id": str(path.id),
            }
        ],
        proposed_by="planner@example.com",
        reason="Review exact source content",
    )
    assert mismatch.ready is False
    assert "does not match" in mismatch.blockers[0]["message"]


def test_batch_review_and_bounded_runs_delegate_without_approving_changes(db_session):
    create_path = _stage_path(db_session, "SPAN-BATCH-CREATE")
    reject_path = _stage_path(db_session, "SPAN-BATCH-REJECT")
    cabinet = FdhCabinet(name="Batch cabinet", code="BATCH-CAB")
    access_point = FiberAccessPoint(name="Batch FAT", code="BATCH-FAT")
    db_session.add_all([cabinet, access_point])
    db_session.commit()

    proposed = propose_connectivity_batch(
        db_session,
        [
            _create_item(create_path, cabinet, access_point),
            _reject_item(reject_path),
        ],
        proposed_by="planner@example.com",
        reason="Field-verified batch",
        source_name="field-walk-2026-07-17",
    )
    assert proposed.created is True
    assert len(proposed.decision_ids) == 2

    review = attest_connectivity_batch(
        db_session,
        proposed.batch_id,
        expected_manifest_sha256=proposed.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Checked exact sources and endpoint IDs",
    )
    assert review.created is True
    assert db_session.query(FiberTopologyConnectivityBatchReview).count() == 1

    run = execute_connectivity_batch(
        db_session,
        proposed.batch_id,
        expected_manifest_sha256=proposed.manifest_sha256,
        executed_by="executor@example.com",
        limit=2,
    )
    assert run.created is True
    assert run.counts == {
        "endpoint_change_requested": 1,
        "segment_change_requested": 0,
        "applied": 0,
        "closed": 1,
        "error": 0,
    }
    assert run.remaining_actionable_count == 0
    requests = db_session.query(FiberChangeRequest).all()
    assert len(requests) == 2
    assert {request.status for request in requests} == {
        FiberChangeRequestStatus.pending
    }

    reconciliation = reconcile_connectivity_batch(
        db_session,
        proposed.batch_id,
        expected_manifest_sha256=proposed.manifest_sha256,
        finalized_by="reconciler@example.com",
        limit=1,
    )
    assert reconciliation.created is True
    assert reconciliation.counts["endpoint_change_requested"] == 1
    assert reconciliation.remaining_actionable_count == 1
    assert db_session.query(FiberTopologyConnectivityRun).count() == 2

    evidence = inspect_connectivity_batch(db_session, proposed.batch_id)
    assert evidence["manifest_sha256"] == proposed.manifest_sha256
    assert evidence["review"]["action"] == "approve"
    assert [item["run_type"] for item in evidence["runs"]] == [
        "execute",
        "reconcile",
    ]


def test_batch_review_is_all_or_nothing_when_one_source_changes(db_session):
    first = _stage_path(db_session, "SPAN-BATCH-ATOMIC-1")
    second = _stage_path(db_session, "SPAN-BATCH-ATOMIC-2")
    proposed = propose_connectivity_batch(
        db_session,
        [_reject_item(first), _reject_item(second)],
        proposed_by="planner@example.com",
        reason="Reject duplicate drawing paths",
    )
    second.content_sha256 = _sha()
    db_session.commit()

    with pytest.raises(FiberTopologyConnectivityError, match="content changed"):
        attest_connectivity_batch(
            db_session,
            proposed.batch_id,
            expected_manifest_sha256=proposed.manifest_sha256,
            action="approve",
            reviewed_by="reviewer@example.com",
            review_notes="Independent review",
        )

    statuses = {
        decision.status
        for decision in db_session.query(FiberTopologyConnectivityDecision).all()
    }
    assert statuses == {"proposed"}
    assert db_session.query(FiberTopologyConnectivityBatchReview).count() == 0


def test_batch_controls_actor_manifest_and_review_gates(db_session):
    path = _stage_path(db_session, "SPAN-BATCH-GATES")
    proposed = propose_connectivity_batch(
        db_session,
        [_reject_item(path)],
        proposed_by="planner@example.com",
        reason="Reject duplicate path",
    )

    with pytest.raises(FiberTopologyConnectivityReviewError, match="proposer"):
        attest_connectivity_batch(
            db_session,
            proposed.batch_id,
            expected_manifest_sha256=proposed.manifest_sha256,
            action="approve",
            reviewed_by="planner@example.com",
            review_notes="Self review",
        )
    with pytest.raises(FiberTopologyConnectivityReviewError, match="manifest"):
        attest_connectivity_batch(
            db_session,
            proposed.batch_id,
            expected_manifest_sha256=_sha(),
            action="approve",
            reviewed_by="reviewer@example.com",
            review_notes="Independent review",
        )
    with pytest.raises(FiberTopologyConnectivityReviewError, match="attestation"):
        execute_connectivity_batch(
            db_session,
            proposed.batch_id,
            expected_manifest_sha256=proposed.manifest_sha256,
            executed_by="executor@example.com",
        )


def test_blocked_proposal_writes_nothing(db_session):
    path = _stage_path(db_session, "SPAN-BATCH-BLOCKED")
    with pytest.raises(FiberTopologyConnectivityProposalBatchBlocked):
        propose_connectivity_batch(
            db_session,
            [_reject_item(path), _reject_item(path)],
            proposed_by="planner@example.com",
            reason="Duplicate rows are invalid",
        )
    assert db_session.query(FiberTopologyConnectivityProposalBatch).count() == 0
    assert db_session.query(FiberTopologyConnectivityDecision).count() == 0
