from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api import domains_network_fiber
from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestStatus,
)
from app.models.fiber_topology_connectivity import (
    FiberTopologyConnectivityDecision,
    FiberTopologySegmentSourceLink,
    FiberTopologyTerminationResolution,
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
    PonPort,
)
from app.schemas.network import (
    FiberSegmentCreate,
    FiberSegmentUpdate,
    FiberTerminationPointCreate,
    FiberTerminationPointUpdate,
)
from app.services import fiber_change_requests
from app.services.network.fiber_topology_connectivity import (
    FiberTopologyConnectivityError,
    approve_connectivity_decision,
    decline_connectivity_decision,
    execute_connectivity_decision,
    finalize_connectivity_decision,
    propose_connectivity_decision,
    reconcile_connectivity_change_requests,
)


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage_path(
    db_session,
    *,
    external_id: str | None = None,
    display_name: str | None = None,
) -> FiberTopologyStagedFeature:
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_path_{uuid.uuid4().hex}",
        source_name="pytest-path.kmz",
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
        external_id=external_id or f"SPAN-{uuid.uuid4().hex[:12]}",
        display_name=display_name or f"Test span {uuid.uuid4().hex[:8]}",
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


def test_reviewed_path_waits_for_terminations_then_segment_change_request(
    db_session, subscriber, olt_device, monkeypatch
):
    path = _stage_path(
        db_session, external_id="SPAN-REVIEWED-1", display_name="Reviewed span 1"
    )
    olt_device.is_active = True
    pon = PonPort(olt_id=olt_device.id, name="0/1/0", is_active=True)
    access_point = FiberAccessPoint(name="Reviewed FAT", code="REVIEWED-FAT")
    db_session.add_all([pon, access_point])
    db_session.commit()

    decision = propose_connectivity_decision(
        db_session,
        path.id,
        "create",
        proposed_by="planner@example.com",
        reason="Field-verified endpoints for the staged path",
        start_endpoint_type="pon_port",
        start_endpoint_ref_id=pon.id,
        end_endpoint_type="fiber_access_point",
        end_endpoint_ref_id=access_point.id,
        segment_type="distribution",
        cable_type="underground",
        fiber_count=24,
    )
    replay = propose_connectivity_decision(
        db_session,
        path.id,
        "create",
        proposed_by="planner@example.com",
        reason="Field-verified endpoints for the staged path",
        start_endpoint_type="pon_port",
        start_endpoint_ref_id=pon.id,
        end_endpoint_type="fiber_access_point",
        end_endpoint_ref_id=access_point.id,
        segment_type="distribution",
        cable_type="underground",
        fiber_count=24,
    )
    assert replay.id == decision.id

    with pytest.raises(FiberTopologyConnectivityError, match="proposer cannot review"):
        approve_connectivity_decision(
            db_session,
            decision.id,
            reviewed_by="planner@example.com",
            review_notes="Self review is forbidden",
        )
    approve_connectivity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Endpoint identities and route evidence verified",
    )
    executed = execute_connectivity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )

    assert executed.status == "endpoint_change_requested"
    assert db_session.query(FiberTerminationPoint).count() == 0
    assert db_session.query(FiberSegment).count() == 0
    endpoint_requests = (
        db_session.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_termination_point")
        .all()
    )
    assert len(endpoint_requests) == 2
    assert {request.status for request in endpoint_requests} == {
        FiberChangeRequestStatus.pending
    }
    assert db_session.query(FiberTopologyTerminationResolution).count() == 2

    for request in endpoint_requests:
        fiber_change_requests.approve_request(
            db_session,
            str(request.id),
            reviewer_person_id=str(subscriber.id),
            review_notes="Canonical termination approved",
        )
    endpoint_result = reconcile_connectivity_change_requests(
        db_session, finalized_by="reconciler@example.com"
    )
    db_session.refresh(decision)

    assert endpoint_result.segment_pending == 1
    assert decision.status == "segment_change_requested"
    assert db_session.query(FiberTerminationPoint).count() == 2
    assert db_session.query(FiberSegment).count() == 0
    segment_request = (
        db_session.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_segment")
        .one()
    )
    assert segment_request.status == FiberChangeRequestStatus.pending
    assert segment_request.payload["from_point_id"]
    assert segment_request.payload["to_point_id"]
    assert segment_request.payload["geojson"] == path.geometry_geojson

    monkeypatch.setattr(
        fiber_change_requests,
        "_geojson_to_geom",
        lambda _value: "LINESTRING(7.40 9.00, 7.42 9.02)",
    )
    fiber_change_requests.approve_request(
        db_session,
        str(segment_request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical segment approved",
    )
    final_result = reconcile_connectivity_change_requests(
        db_session, finalized_by="reconciler@example.com"
    )
    db_session.refresh(decision)

    assert final_result.applied == 1
    assert decision.status == "applied"
    assert decision.canonical_segment_id is not None
    segment = db_session.get(FiberSegment, decision.canonical_segment_id)
    assert segment is not None
    assert segment.from_point_id != segment.to_point_id
    assert segment.route_geom is not None
    source_link = db_session.query(FiberTopologySegmentSourceLink).one()
    assert source_link.external_id == "SPAN-REVIEWED-1"
    assert source_link.content_sha256 == path.content_sha256
    assert source_link.segment_id == segment.id


def test_shared_endpoint_reuses_one_pending_termination_resolution(db_session):
    first_path = _stage_path(db_session, external_id="SPAN-SHARED-1")
    second_path = _stage_path(db_session, external_id="SPAN-SHARED-2")
    cabinet = FdhCabinet(name="Shared cabinet", code="SHARED-CAB")
    first_access = FiberAccessPoint(name="First FAT", code="SHARED-FAT-1")
    second_access = FiberAccessPoint(name="Second FAT", code="SHARED-FAT-2")
    db_session.add_all([cabinet, first_access, second_access])
    db_session.commit()

    decisions = []
    for path, endpoint in (
        (first_path, first_access),
        (second_path, second_access),
    ):
        decision = propose_connectivity_decision(
            db_session,
            path.id,
            "create",
            proposed_by="planner@example.com",
            reason=f"Reviewed shared endpoint for {path.external_id}",
            start_endpoint_type="fdh",
            start_endpoint_ref_id=cabinet.id,
            end_endpoint_type="fiber_access_point",
            end_endpoint_ref_id=endpoint.id,
            fiber_count=12,
        )
        approve_connectivity_decision(
            db_session,
            decision.id,
            reviewed_by="reviewer@example.com",
            review_notes="Shared endpoint independently verified",
        )
        decisions.append(
            execute_connectivity_decision(
                db_session, decision.id, executed_by="executor@example.com"
            )
        )

    assert {decision.status for decision in decisions} == {"endpoint_change_requested"}
    assert db_session.query(FiberTopologyTerminationResolution).count() == 3
    assert (
        db_session.query(FiberTopologyTerminationResolution)
        .filter(
            FiberTopologyTerminationResolution.endpoint_type == "fdh",
            FiberTopologyTerminationResolution.endpoint_ref_id == cabinet.id,
        )
        .count()
        == 1
    )
    assert db_session.query(FiberChangeRequest).count() == 3


def test_existing_operational_segment_can_be_linked_without_mutation(
    db_session, olt_device
):
    path = _stage_path(db_session, external_id="SPAN-LINK-1")
    olt_device.is_active = True
    pon = PonPort(olt_id=olt_device.id, name="0/1/1", is_active=True)
    closure = FiberSpliceClosure(name="Link closure")
    db_session.add_all([pon, closure])
    db_session.commit()
    start = FiberTerminationPoint(
        name="PON termination",
        endpoint_type=ODNEndpointType.pon_port,
        ref_id=pon.id,
    )
    end = FiberTerminationPoint(
        name="Closure termination",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
    )
    segment = FiberSegment(
        name="Existing reviewed segment",
        from_point=start,
        to_point=end,
        route_geom="LINESTRING(7.40 9.00, 7.42 9.02)",
        fiber_count=24,
    )
    db_session.add(segment)
    db_session.commit()

    decision = propose_connectivity_decision(
        db_session,
        path.id,
        "link_existing",
        target_segment_id=segment.id,
        proposed_by="planner@example.com",
        reason="The installed segment matches the staged source identity",
    )
    approve_connectivity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Existing segment endpoints and geometry verified",
    )
    applied = execute_connectivity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )

    assert applied.status == "applied"
    assert applied.canonical_segment_id == segment.id
    assert db_session.query(FiberChangeRequest).count() == 0
    assert (
        db_session.query(FiberTopologySegmentSourceLink).one().segment_id == segment.id
    )


def test_reject_and_endpoint_rejection_preserve_terminal_evidence(
    db_session, subscriber
):
    reject_path = _stage_path(db_session, external_id="SPAN-REJECT-1")
    rejected = propose_connectivity_decision(
        db_session,
        reject_path.id,
        "reject",
        proposed_by="planner@example.com",
        reason="Duplicate survey path is not installed plant",
    )
    approve_connectivity_decision(
        db_session,
        rejected.id,
        reviewed_by="reviewer@example.com",
        review_notes="Rejection independently verified",
    )
    closed = execute_connectivity_decision(
        db_session, rejected.id, executed_by="executor@example.com"
    )
    assert closed.status == "closed"
    assert closed.closed_reason == "source_path_rejected"

    create_path = _stage_path(db_session, external_id="SPAN-ENDPOINT-REJECT-1")
    cabinet = FdhCabinet(name="Rejected endpoint cabinet", code="REJECT-CAB")
    access_point = FiberAccessPoint(name="Rejected endpoint FAT", code="REJECT-FAT")
    db_session.add_all([cabinet, access_point])
    db_session.commit()
    decision = propose_connectivity_decision(
        db_session,
        create_path.id,
        "create",
        proposed_by="planner@example.com",
        reason="Endpoint request rejection test",
        start_endpoint_type="fdh",
        start_endpoint_ref_id=cabinet.id,
        end_endpoint_type="fiber_access_point",
        end_endpoint_ref_id=access_point.id,
        fiber_count=12,
    )
    approve_connectivity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Endpoint identities reviewed",
    )
    execute_connectivity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )
    request = (
        db_session.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_termination_point")
        .first()
    )
    assert request is not None
    fiber_change_requests.reject_request(
        db_session,
        str(request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Endpoint mutation rejected",
    )
    finalized = finalize_connectivity_decision(
        db_session, decision.id, finalized_by="reconciler@example.com"
    )
    assert finalized.status == "closed"
    assert finalized.closed_reason == "endpoint_change_request_rejected"
    assert db_session.query(FiberSegment).count() == 0


def test_newer_path_content_blocks_review_then_decline_allows_latest_proposal(
    db_session,
):
    old_path = _stage_path(db_session, external_id="SPAN-STALE-1")
    decision = propose_connectivity_decision(
        db_session,
        old_path.id,
        "reject",
        proposed_by="planner@example.com",
        reason="Initial path review",
    )
    new_path = _stage_path(
        db_session,
        external_id="SPAN-STALE-1",
        display_name="Changed path source",
    )

    with pytest.raises(FiberTopologyConnectivityError, match="newer staged version"):
        approve_connectivity_decision(
            db_session,
            decision.id,
            reviewed_by="reviewer@example.com",
            review_notes="Stale decision cannot be approved",
        )
    decline_connectivity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Superseded by newer source evidence",
    )
    corrected = propose_connectivity_decision(
        db_session,
        new_path.id,
        "reject",
        proposed_by="planner@example.com",
        reason="Latest path source reviewed",
    )
    assert corrected.id != decision.id
    assert corrected.status == "proposed"
    assert db_session.query(FiberTopologyConnectivityDecision).count() == 2


def test_source_change_after_endpoint_requests_prevents_segment_emission(
    db_session, subscriber
):
    path = _stage_path(db_session, external_id="SPAN-CHANGED-BEFORE-SEGMENT")
    cabinet = FdhCabinet(name="Drift cabinet", code="DRIFT-CAB")
    access_point = FiberAccessPoint(name="Drift FAT", code="DRIFT-FAT")
    db_session.add_all([cabinet, access_point])
    db_session.commit()
    decision = propose_connectivity_decision(
        db_session,
        path.id,
        "create",
        proposed_by="planner@example.com",
        reason="Path current before endpoint requests",
        start_endpoint_type="fdh",
        start_endpoint_ref_id=cabinet.id,
        end_endpoint_type="fiber_access_point",
        end_endpoint_ref_id=access_point.id,
        fiber_count=12,
    )
    approve_connectivity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Original path evidence independently verified",
    )
    execute_connectivity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )
    _stage_path(
        db_session,
        external_id="SPAN-CHANGED-BEFORE-SEGMENT",
        display_name="Changed before segment request",
    )
    endpoint_requests = (
        db_session.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_termination_point")
        .all()
    )
    for request in endpoint_requests:
        fiber_change_requests.approve_request(
            db_session,
            str(request.id),
            reviewer_person_id=str(subscriber.id),
            review_notes="Endpoint itself remains valid",
        )

    finalized = finalize_connectivity_decision(
        db_session, decision.id, finalized_by="reconciler@example.com"
    )

    assert finalized.status == "closed"
    assert finalized.closed_reason == "source_changed_before_segment_request"
    assert (
        db_session.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_segment")
        .count()
        == 0
    )
    assert db_session.query(FiberTerminationPoint).count() == 2


def test_direct_api_termination_and_segment_mutations_are_gone(db_session):
    point_id = str(uuid.uuid4())
    segment_id = str(uuid.uuid4())
    calls = (
        lambda: domains_network_fiber.create_fiber_termination_point(
            FiberTerminationPointCreate(name="Retired direct point"), db_session
        ),
        lambda: domains_network_fiber.update_fiber_termination_point(
            point_id,
            FiberTerminationPointUpdate(name="Retired update"),
            db_session,
        ),
        lambda: domains_network_fiber.delete_fiber_termination_point(
            point_id, db_session
        ),
        lambda: domains_network_fiber.create_fiber_segment(
            FiberSegmentCreate(name="Retired direct segment"), db_session
        ),
        lambda: domains_network_fiber.update_fiber_segment(
            segment_id,
            FiberSegmentUpdate(name="Retired segment update"),
            db_session,
        ),
        lambda: domains_network_fiber.delete_fiber_segment(segment_id, db_session),
    )
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 410
        assert "reviewed fiber connectivity decision" in exc.value.detail
