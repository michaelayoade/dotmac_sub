from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.fiber_change_request import FiberChangeRequestOperation
from app.models.fiber_support import (
    FiberSupportMount,
    FiberSupportMountDecision,
    FiberSupportStructure,
)
from app.models.network import (
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
    FiberTerminationPoint,
    ODNEndpointType,
    PonPort,
)
from app.services import fiber_change_requests
from app.services.network import fiber_support_structures
from app.services.network.fiber_support_structures import (
    FiberSupportStructureError,
    execute_mount_decision,
    inspect_mount_decision,
    preview_mount_decision,
    propose_mount_decision,
    review_mount_decision,
)


def _support(
    db_session, subscriber, monkeypatch, *, code: str
) -> FiberSupportStructure:
    monkeypatch.setattr(
        fiber_support_structures, "_geojson_to_geom", lambda _value: None
    )
    request = fiber_change_requests.create_request(
        db_session,
        asset_type="support_structure",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload={
            "code": code,
            "name": f"Support {code}",
            "support_type": "pole",
            "ownership_status": "third_party",
            "lifecycle_status": "active",
            "inspection_status": "passed",
            "lease_status": "active",
            "geojson": {"type": "Point", "coordinates": [7.4, 9.0]},
        },
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    applied = fiber_change_requests.approve_request(
        db_session,
        str(request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical support identity and state reviewed",
    )
    support = db_session.get(FiberSupportStructure, applied.asset_id)
    assert support is not None
    return support


def _mount_args(
    support: FiberSupportStructure,
    asset: FiberAccessPoint | FiberSegment,
    *,
    action: str = "attach",
    mount: FiberSupportMount | None = None,
) -> dict:
    is_segment = isinstance(asset, FiberSegment)
    return {
        "action": action,
        "support_structure_id": support.id,
        "mounted_asset_type": ("fiber_segment" if is_segment else "fiber_access_point"),
        "mounted_asset_id": asset.id,
        "mount_role": "route_support" if is_segment else "hosted",
        "sequence": 1 if is_segment else None,
        "existing_mount_id": mount.id if mount else None,
        "reason": "Verified exact installed support edge",
        "proposed_by": "planner@example.com",
    }


def test_reviewed_change_owner_delegates_canonical_support_mutation(
    db_session, subscriber, monkeypatch
):
    support = _support(db_session, subscriber, monkeypatch, code="POLE-SOT-001")

    assert support.code == "POLE-SOT-001"
    assert support.support_type == "pole"
    assert support.ownership_status == "third_party"
    assert support.inspection_status == "passed"
    assert support.lease_status == "active"
    assert support.is_active is True

    update = fiber_change_requests.create_request(
        db_session,
        asset_type="support_structure",
        asset_id=str(support.id),
        operation=FiberChangeRequestOperation.update,
        payload={
            "inspection_status": "due",
            "lease_status": "expired",
        },
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    fiber_change_requests.approve_request(
        db_session,
        str(update.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Inspection and lease evidence independently reviewed",
    )
    db_session.refresh(support)
    assert support.inspection_status == "due"
    assert support.lease_status == "expired"


def test_exact_mount_preview_review_execution_and_detach_are_audited(
    db_session, subscriber, monkeypatch
):
    support = _support(db_session, subscriber, monkeypatch, code="POLE-SOT-EDGE")
    asset = FiberAccessPoint(name="Canonical FAT for support test")
    db_session.add(asset)
    db_session.commit()
    args = _mount_args(support, asset)

    preview = preview_mount_decision(db_session, **args)
    assert db_session.query(FiberSupportMountDecision).count() == 0
    proposed = propose_mount_decision(
        db_session,
        expected_decision_sha256=preview.decision_sha256,
        **args,
    )
    replay = propose_mount_decision(
        db_session,
        expected_decision_sha256=preview.decision_sha256,
        **args,
    )
    assert replay.id == proposed.id

    with pytest.raises(FiberSupportStructureError, match="proposer cannot review"):
        review_mount_decision(
            db_session,
            proposed.id,
            action="approve",
            reviewed_by="planner@example.com",
            review_notes="Invalid self review",
            expected_decision_sha256=preview.decision_sha256,
        )

    review_mount_decision(
        db_session,
        proposed.id,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Support and exact FAT identity independently verified",
        expected_decision_sha256=preview.decision_sha256,
    )
    applied = execute_mount_decision(
        db_session,
        proposed.id,
        executed_by="executor@example.com",
        expected_decision_sha256=preview.decision_sha256,
    )
    replayed = execute_mount_decision(
        db_session,
        proposed.id,
        executed_by="another-executor@example.com",
        expected_decision_sha256=preview.decision_sha256,
    )
    mount = db_session.get(FiberSupportMount, applied.result_mount_id)

    assert replayed.id == applied.id
    assert applied.status == "applied"
    assert mount is not None and mount.is_active
    assert mount.support_structure_id == support.id
    assert mount.mounted_asset_id == asset.id
    assert inspect_mount_decision(db_session, applied.id)["result_valid"] is True
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(proposed.id))
        .count()
        == 3
    )

    detach_args = _mount_args(support, asset, action="detach", mount=mount)
    detach_preview = preview_mount_decision(db_session, **detach_args)
    detach = propose_mount_decision(
        db_session,
        expected_decision_sha256=detach_preview.decision_sha256,
        **detach_args,
    )
    review_mount_decision(
        db_session,
        detach.id,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Removal independently verified",
        expected_decision_sha256=detach_preview.decision_sha256,
    )
    execute_mount_decision(
        db_session,
        detach.id,
        executed_by="executor@example.com",
        expected_decision_sha256=detach_preview.decision_sha256,
    )
    db_session.refresh(mount)
    assert mount.is_active is False
    assert mount.removed_by == "executor@example.com"
    assert inspect_mount_decision(db_session, detach.id)["result_valid"] is True
    historical_attach = inspect_mount_decision(db_session, applied.id)
    assert historical_attach["result_valid"] is True
    assert historical_attach["result_current"] is False


def test_mount_review_fails_closed_when_support_state_changes(
    db_session, subscriber, monkeypatch
):
    support = _support(db_session, subscriber, monkeypatch, code="POLE-SOT-STALE")
    asset = FiberAccessPoint(name="FAT stale support evidence")
    db_session.add(asset)
    db_session.commit()
    args = _mount_args(support, asset)
    preview = preview_mount_decision(db_session, **args)
    decision = propose_mount_decision(
        db_session,
        expected_decision_sha256=preview.decision_sha256,
        **args,
    )

    support.inspection_status = "failed"
    db_session.commit()
    with pytest.raises(FiberSupportStructureError, match="evidence changed"):
        review_mount_decision(
            db_session,
            decision.id,
            action="approve",
            reviewed_by="reviewer@example.com",
            review_notes="This must not approve stale support state",
            expected_decision_sha256=preview.decision_sha256,
        )


def test_mount_execution_records_terminal_stale_evidence(
    db_session, subscriber, monkeypatch
):
    support = _support(db_session, subscriber, monkeypatch, code="POLE-SOT-CLOSE")
    asset = FiberAccessPoint(name="FAT with changed execution evidence")
    db_session.add(asset)
    db_session.commit()
    args = _mount_args(support, asset)
    preview = preview_mount_decision(db_session, **args)
    decision = propose_mount_decision(
        db_session,
        expected_decision_sha256=preview.decision_sha256,
        **args,
    )
    review_mount_decision(
        db_session,
        decision.id,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Exact active FAT and support reviewed",
        expected_decision_sha256=preview.decision_sha256,
    )

    asset.is_active = False
    db_session.commit()
    closed = execute_mount_decision(
        db_session,
        decision.id,
        executed_by="executor@example.com",
        expected_decision_sha256=preview.decision_sha256,
    )
    evidence = inspect_mount_decision(db_session, closed.id)

    assert closed.status == "closed"
    assert closed.closed_reason == "authoritative_support_or_asset_inputs_changed"
    assert closed.result_mount_id is None
    assert evidence["result_valid"] is True
    assert evidence["result_current"] is None
    assert evidence["result_payload"]["outcome"] == "closed_stale"


def test_mount_invariants_keep_point_assets_single_and_segments_ordered(
    db_session, subscriber, olt_device, monkeypatch
):
    first = _support(db_session, subscriber, monkeypatch, code="POLE-SOT-ORDER-1")
    second = _support(db_session, subscriber, monkeypatch, code="POLE-SOT-ORDER-2")
    asset = FiberAccessPoint(name="FAT with one exact support")
    olt_device.is_active = True
    pon = PonPort(olt_id=olt_device.id, name="0/1/ordered-support", is_active=True)
    closure = FiberSpliceClosure(name="Ordered-support closure", is_active=True)
    db_session.add_all([asset, pon, closure])
    db_session.flush()
    start = FiberTerminationPoint(
        name="Ordered-support PON termination",
        endpoint_type=ODNEndpointType.pon_port,
        ref_id=pon.id,
    )
    end = FiberTerminationPoint(
        name="Ordered-support closure termination",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
    )
    segment = FiberSegment(
        name=f"Segment-{uuid.uuid4().hex}",
        from_point=start,
        to_point=end,
        route_geom="LINESTRING(7.40 9.00, 7.42 9.02)",
        fiber_count=12,
        is_active=True,
    )
    db_session.add_all([start, end, segment])
    db_session.commit()

    point_args = _mount_args(first, asset)
    point_preview = preview_mount_decision(db_session, **point_args)
    point_decision = propose_mount_decision(
        db_session,
        expected_decision_sha256=point_preview.decision_sha256,
        **point_args,
    )
    review_mount_decision(
        db_session,
        point_decision.id,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Exact point support reviewed",
        expected_decision_sha256=point_preview.decision_sha256,
    )
    execute_mount_decision(
        db_session,
        point_decision.id,
        executed_by="executor@example.com",
        expected_decision_sha256=point_preview.decision_sha256,
    )
    with pytest.raises(FiberSupportStructureError, match="already has"):
        preview_mount_decision(db_session, **_mount_args(second, asset))

    first_segment_args = _mount_args(first, segment)
    first_segment_preview = preview_mount_decision(db_session, **first_segment_args)
    first_segment = propose_mount_decision(
        db_session,
        expected_decision_sha256=first_segment_preview.decision_sha256,
        **first_segment_args,
    )
    review_mount_decision(
        db_session,
        first_segment.id,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="First ordered route support reviewed",
        expected_decision_sha256=first_segment_preview.decision_sha256,
    )
    execute_mount_decision(
        db_session,
        first_segment.id,
        executed_by="executor@example.com",
        expected_decision_sha256=first_segment_preview.decision_sha256,
    )
    duplicate_sequence = {**_mount_args(second, segment), "sequence": 1}
    with pytest.raises(FiberSupportStructureError, match="sequence"):
        preview_mount_decision(db_session, **duplicate_sequence)


def test_support_retirement_requires_detached_mounts(
    db_session, subscriber, monkeypatch
):
    support = _support(db_session, subscriber, monkeypatch, code="POLE-SOT-RETIRE")
    asset = FiberAccessPoint(name="FAT blocking support retirement")
    db_session.add(asset)
    db_session.commit()
    args = _mount_args(support, asset)
    preview = preview_mount_decision(db_session, **args)
    decision = propose_mount_decision(
        db_session,
        expected_decision_sha256=preview.decision_sha256,
        **args,
    )
    review_mount_decision(
        db_session,
        decision.id,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Exact support mount reviewed",
        expected_decision_sha256=preview.decision_sha256,
    )
    execute_mount_decision(
        db_session,
        decision.id,
        executed_by="executor@example.com",
        expected_decision_sha256=preview.decision_sha256,
    )

    request = fiber_change_requests.create_request(
        db_session,
        asset_type="support_structure",
        asset_id=str(support.id),
        operation=FiberChangeRequestOperation.delete,
        payload={},
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    with pytest.raises(HTTPException, match="detach every active mount"):
        fiber_change_requests.approve_request(
            db_session,
            str(request.id),
            reviewer_person_id=str(subscriber.id),
            review_notes="Cannot retire a load-bearing support",
        )
