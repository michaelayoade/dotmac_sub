from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.fiber_change_request import FiberChangeRequestOperation
from app.models.network import (
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
    FiberStrand,
    FiberTerminationPoint,
    ODNEndpointType,
    PonPort,
    Splitter,
    SplitterPortType,
)
from app.schemas.network import SplitterCreate, SplitterPortCreate
from app.services import fiber_change_requests
from app.services.network.fiber_plant_integrity import (
    cable_capacity,
    splitter_capacity,
)
from app.services.network.splitters import splitter_ports, splitters


def _point(db, endpoint_type, ref_id, name):
    point = FiberTerminationPoint(
        name=name,
        endpoint_type=endpoint_type,
        ref_id=ref_id,
        is_active=True,
    )
    db.add(point)
    db.flush()
    return point


def _segment_request(
    db, start, end, *, name, fiber_count=12, operation="create", asset_id=None
):
    payload = (
        {
            "fiber_count": fiber_count,
            "from_point_id": str(start.id),
            "is_active": True,
            "name": name,
            "route_geom": "LINESTRING(7.40 9.00, 7.42 9.02)",
            "segment_type": "distribution",
            "to_point_id": str(end.id),
        }
        if operation == "create"
        else {"fiber_count": fiber_count}
    )
    return fiber_change_requests.create_request(
        db,
        asset_type="fiber_segment",
        asset_id=str(asset_id) if asset_id else None,
        operation=FiberChangeRequestOperation(operation),
        payload=payload,
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )


def _approve(db, request, reviewer_id):
    return fiber_change_requests.approve_request(
        db,
        str(request.id),
        reviewer_person_id=str(reviewer_id),
        review_notes="Exact passive plant and declared capacity reviewed",
    )


def _rooted_segment(db, subscriber, olt_device, *, fiber_count=12):
    olt_device.is_active = True
    pon = PonPort(olt_id=olt_device.id, name=f"pon-{uuid.uuid4().hex}", is_active=True)
    closure = FiberSpliceClosure(name=f"closure-{uuid.uuid4().hex}", is_active=True)
    db.add_all([pon, closure])
    db.flush()
    start = _point(db, ODNEndpointType.pon_port, pon.id, "PON termination")
    end = _point(db, ODNEndpointType.splice_closure, closure.id, "Closure termination")
    request = _segment_request(
        db,
        start,
        end,
        name=f"rooted-{uuid.uuid4().hex}",
        fiber_count=fiber_count,
    )
    applied = _approve(db, request, subscriber.id)
    segment = db.get(FiberSegment, applied.asset_id)
    assert segment is not None
    return segment, end


def test_active_cable_requires_a_serving_pon_root(db_session, subscriber):
    closure = FiberSpliceClosure(name="Unrooted closure")
    access = FiberAccessPoint(name="Unrooted FAT")
    db_session.add_all([closure, access])
    db_session.flush()
    start = _point(
        db_session,
        ODNEndpointType.splice_closure,
        closure.id,
        "Unrooted closure termination",
    )
    end = _point(
        db_session,
        ODNEndpointType.fiber_access_point,
        access.id,
        "Unrooted FAT termination",
    )
    request = _segment_request(
        db_session, start, end, name=f"unrooted-{uuid.uuid4().hex}"
    )

    with pytest.raises(HTTPException, match="serving PON/OLT root"):
        _approve(db_session, request, subscriber.id)


def test_reviewed_cable_size_materializes_exact_numbered_cores(
    db_session, subscriber, olt_device
):
    segment, _end = _rooted_segment(db_session, subscriber, olt_device, fiber_count=12)

    capacity = cable_capacity(db_session, segment.id)

    assert capacity.total_fibers == 12
    assert capacity.modeled_fibers == 12
    assert capacity.available_fibers == 12
    assert capacity.complete is True

    shrink = _segment_request(
        db_session,
        None,
        None,
        name="ignored",
        fiber_count=6,
        operation="update",
        asset_id=segment.id,
    )
    with pytest.raises(HTTPException, match="smaller than its exact numbered"):
        _approve(db_session, shrink, subscriber.id)


def test_name_only_legacy_strands_require_reviewed_exact_assignment(
    db_session, subscriber, olt_device
):
    olt_device.is_active = True
    pon = PonPort(olt_id=olt_device.id, name="pon-legacy-core", is_active=True)
    closure = FiberSpliceClosure(name="legacy-core-closure", is_active=True)
    db_session.add_all([pon, closure])
    db_session.flush()
    start = _point(db_session, ODNEndpointType.pon_port, pon.id, "Legacy PON")
    end = _point(
        db_session,
        ODNEndpointType.splice_closure,
        closure.id,
        "Legacy closure",
    )
    cable_name = f"legacy-name-only-{uuid.uuid4().hex}"
    db_session.add(FiberStrand(cable_name=cable_name, strand_number=1))
    db_session.flush()
    request = _segment_request(db_session, start, end, name=cable_name)

    with pytest.raises(HTTPException, match="reviewed exact segment assignment"):
        _approve(db_session, request, subscriber.id)


def test_leaf_first_retirement_cannot_orphan_active_cable(
    db_session, subscriber, olt_device
):
    root, closure_point = _rooted_segment(db_session, subscriber, olt_device)
    access = FiberAccessPoint(name="Downstream FAT")
    db_session.add(access)
    db_session.flush()
    access_point = _point(
        db_session,
        ODNEndpointType.fiber_access_point,
        access.id,
        "Downstream FAT termination",
    )
    leaf_request = _segment_request(
        db_session,
        closure_point,
        access_point,
        name=f"leaf-{uuid.uuid4().hex}",
        fiber_count=4,
    )
    leaf = db_session.get(
        FiberSegment, _approve(db_session, leaf_request, subscriber.id).asset_id
    )
    assert leaf is not None

    retire_root = fiber_change_requests.create_request(
        db_session,
        asset_type="fiber_segment",
        asset_id=str(root.id),
        operation=FiberChangeRequestOperation.delete,
        payload={},
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    with pytest.raises(HTTPException, match="orphan an active cable component"):
        _approve(db_session, retire_root, subscriber.id)

    retire_leaf = fiber_change_requests.create_request(
        db_session,
        asset_type="fiber_segment",
        asset_id=str(leaf.id),
        operation=FiberChangeRequestOperation.delete,
        payload={},
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    _approve(db_session, retire_leaf, subscriber.id)
    db_session.refresh(leaf)
    assert leaf.is_active is False


def test_splitter_ratio_declares_capacity_and_ports_cannot_exceed_it(db_session):
    splitter = splitters.create(
        db_session,
        SplitterCreate(name="Capacity splitter", splitter_ratio="1:2"),
    )
    assert splitter.input_ports == 1
    assert splitter.output_ports == 2
    splitter_ports.create(
        db_session,
        SplitterPortCreate(
            splitter_id=splitter.id,
            port_number=1,
            port_type=SplitterPortType.output,
        ),
    )
    splitter_ports.create(
        db_session,
        SplitterPortCreate(
            splitter_id=splitter.id,
            port_number=2,
            port_type=SplitterPortType.output,
        ),
    )

    with pytest.raises(HTTPException, match="capacity 2 would be exceeded"):
        splitter_ports.create(
            db_session,
            SplitterPortCreate(
                splitter_id=splitter.id,
                port_number=3,
                port_type=SplitterPortType.output,
            ),
        )

    capacity = splitter_capacity(db_session, splitter.id)
    assert capacity.output_capacity == 2
    assert capacity.modeled_outputs == 2
    assert capacity.occupied_outputs == 0
    assert capacity.spare_outputs == 2


def test_reviewed_splitter_changes_delegate_to_the_capacity_owner(
    db_session, subscriber
):
    request = fiber_change_requests.create_request(
        db_session,
        asset_type="splitter",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload={"name": "Reviewed capacity splitter", "splitter_ratio": "1:4"},
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    applied = _approve(db_session, request, subscriber.id)
    splitter = db_session.get(Splitter, applied.asset_id)

    assert splitter is not None
    assert splitter.input_ports == 1
    assert splitter.output_ports == 4

    invalid = fiber_change_requests.create_request(
        db_session,
        asset_type="splitter",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload={
            "name": "Conflicting reviewed capacity splitter",
            "splitter_ratio": "1:8",
            "input_ports": 1,
            "output_ports": 4,
        },
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    with pytest.raises(HTTPException, match="declared input/output capacity 1:4"):
        _approve(db_session, invalid, subscriber.id)
