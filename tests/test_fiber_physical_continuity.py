from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.fiber_change_request import FiberChangeRequestOperation
from app.models.fiber_physical import (
    FiberConnectorPort,
    FiberPatchCord,
    FiberPatchPanel,
    FiberRack,
    FiberStrandTermination,
)
from app.models.network import (
    FiberSegment,
    FiberSpliceClosure,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    ODNEndpointType,
)
from app.models.network_monitoring import PopSite
from app.services import fiber_change_requests
from app.services.network.fiber_physical_continuity import (
    FiberPhysicalContinuityError,
    _add_edge,
    _GraphEdge,
    _path_has_alternate_route,
    approve_physical_link,
    execute_physical_link,
    preview_physical_link,
    propose_physical_link,
    resolve_core_continuity,
    resolve_subscription_core_continuity,
)
from app.services.network.fiber_plant_integrity import ensure_segment_strand_inventory
from tests.test_fiber_subscription_trace import _complete_path


def _reviewed_inventory(
    db,
    subscriber,
    *,
    asset_type: str,
    payload: dict,
):
    request = fiber_change_requests.create_request(
        db,
        asset_type=asset_type,
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload=payload,
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    applied = fiber_change_requests.approve_request(
        db,
        str(request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Exact physical inventory and capacity independently reviewed",
    )
    return applied.asset_id


def _connect(db, link_type: str, **values):
    decision = propose_physical_link(
        db,
        link_type,
        "connect",
        proposed_by="planner@example.com",
        reason="Exact installed optical continuity independently verified",
        **values,
    )
    approve_physical_link(
        db,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Connector, strand end, location, and labels checked",
    )
    applied = execute_physical_link(
        db,
        decision.id,
        executed_by="executor@example.com",
    )
    assert applied.status == "applied"
    assert applied.result_payload["link_id"] is not None
    return applied


def _connector(**owner) -> FiberConnectorPort:
    return FiberConnectorPort(
        **owner,
        connector_type="sc",
        polish_type="apc",
        fiber_mode="single_mode",
        is_active=True,
    )


def test_path_ambiguity_detects_a_longer_alternate_route():
    nodes = [("connector", uuid.uuid4()) for _ in range(5)]
    direct_first = _GraphEdge("patch_cord", uuid.uuid4())
    direct_second = _GraphEdge("patch_cord", uuid.uuid4())
    alternate = [_GraphEdge("patch_cord", uuid.uuid4()) for _ in range(3)]
    adjacency = {}
    _add_edge(adjacency, nodes[0], nodes[1], direct_first)
    _add_edge(adjacency, nodes[1], nodes[2], direct_second)
    _add_edge(adjacency, nodes[0], nodes[3], alternate[0])
    _add_edge(adjacency, nodes[3], nodes[4], alternate[1])
    _add_edge(adjacency, nodes[4], nodes[2], alternate[2])

    assert _path_has_alternate_route(
        adjacency,
        nodes[0],
        [direct_first, direct_second],
    )

    unique_adjacency = {}
    _add_edge(unique_adjacency, nodes[0], nodes[1], direct_first)
    _add_edge(unique_adjacency, nodes[1], nodes[2], direct_second)
    assert not _path_has_alternate_route(
        unique_adjacency,
        nodes[0],
        [direct_first, direct_second],
    )


def install_complete_core_path(db, assets, network_device):
    """Install exact POP ODF patching and direct leaf/drop terminations."""

    for segment in (assets["feeder"], assets["drop"]):
        ensure_segment_strand_inventory(db, segment)
    feeder_core = db.scalar(
        db.query(FiberStrand)
        .filter(
            FiberStrand.segment_id == assets["feeder"].id,
            FiberStrand.strand_number == 1,
        )
        .statement
    )
    drop_core = db.scalar(
        db.query(FiberStrand)
        .filter(
            FiberStrand.segment_id == assets["drop"].id,
            FiberStrand.strand_number == 1,
        )
        .statement
    )
    assert feeder_core is not None and drop_core is not None
    feeder_core.status = FiberStrandStatus.in_use
    drop_core.status = FiberStrandStatus.in_use

    rack = FiberRack(
        code=f"RACK-{uuid.uuid4().hex[:10]}",
        name="Serving POP fiber rack",
        pop_site_id=network_device.pop_site_id,
        rack_units=42,
        is_active=True,
    )
    db.add(rack)
    db.flush()
    odf = FiberPatchPanel(
        rack_id=rack.id,
        name="OLT ODF 01",
        panel_type="odf",
        rack_unit_start=1,
        rack_unit_height=1,
        port_capacity=24,
        connector_type="sc",
        polish_type="apc",
        fiber_mode="single_mode",
        is_active=True,
    )
    db.add(odf)
    db.flush()
    odf_port = _connector(
        patch_panel_id=odf.id,
        port_number=1,
        label="ODF-01/01",
    )
    pon_connector = _connector(
        pon_port_id=assets["pon"].id,
        label="OLT PON optical port",
    )
    input_connector = _connector(
        splitter_port_id=assets["input"].id,
        label="Root splitter input",
    )
    output_connector = _connector(
        splitter_port_id=assets["output"].id,
        label="Leaf splitter output",
    )
    ont_connector = _connector(
        ont_unit_id=assets["ont"].id,
        label="Customer ONT optical port",
    )
    db.add_all(
        [
            odf_port,
            pon_connector,
            input_connector,
            output_connector,
            ont_connector,
        ]
    )
    db.commit()

    feeder_from_type = assets["feeder"].from_point.endpoint_type.value
    assert feeder_from_type == "pon_port"
    _connect(
        db,
        "strand_termination",
        first_strand_id=feeder_core.id,
        first_strand_end="a",
        connector_port_id=odf_port.id,
    )
    patch = _connect(
        db,
        "patch_cord",
        first_connector_port_id=odf_port.id,
        second_connector_port_id=pon_connector.id,
        label="OLT-PON-01 to ODF-01/01",
        assembly_label="PC-OLT-0001",
        length_m="3.0",
        insertion_loss_db="0.2",
    )
    _connect(
        db,
        "strand_termination",
        first_strand_id=feeder_core.id,
        first_strand_end="b",
        connector_port_id=input_connector.id,
    )
    _connect(
        db,
        "strand_termination",
        first_strand_id=drop_core.id,
        first_strand_end="a",
        connector_port_id=output_connector.id,
    )
    _connect(
        db,
        "strand_termination",
        first_strand_id=drop_core.id,
        first_strand_end="b",
        connector_port_id=ont_connector.id,
    )
    return {
        "rack": rack,
        "odf": odf,
        "odf_port": odf_port,
        "patch_decision": patch,
        "feeder_core": feeder_core,
        "drop_core": drop_core,
    }


def test_reviewed_rack_panel_and_port_capacity_are_exact(
    db_session, subscriber, network_device
):
    site = db_session.get(PopSite, network_device.pop_site_id)
    assert site is not None
    rack_id = _reviewed_inventory(
        db_session,
        subscriber,
        asset_type="fiber_rack",
        payload={
            "code": f"RACK-{uuid.uuid4().hex[:10]}",
            "name": "Reviewed fiber rack",
            "pop_site_id": str(site.id),
            "rack_units": 12,
        },
    )
    rack = db_session.get(FiberRack, rack_id)
    assert rack is not None
    panel_id = _reviewed_inventory(
        db_session,
        subscriber,
        asset_type="fiber_patch_panel",
        payload={
            "rack_id": str(rack.id),
            "name": "Reviewed 12-port ODF",
            "panel_type": "odf",
            "rack_unit_start": 2,
            "rack_unit_height": 1,
            "port_capacity": 12,
            "connector_type": "sc",
            "polish_type": "apc",
            "fiber_mode": "single_mode",
        },
    )
    panel = db_session.get(FiberPatchPanel, panel_id)
    assert panel is not None

    for connector_type, rack_unit in (("mpo", 3), ("mtp", 4)):
        unsupported_multifiber_panel = fiber_change_requests.create_request(
            db_session,
            asset_type="fiber_patch_panel",
            asset_id=None,
            operation=FiberChangeRequestOperation.create,
            payload={
                "rack_id": str(rack.id),
                "name": f"Unsupported {connector_type.upper()} panel",
                "panel_type": "patch_panel",
                "rack_unit_start": rack_unit,
                "rack_unit_height": 1,
                "port_capacity": 12,
                "connector_type": connector_type,
                "polish_type": "apc",
                "fiber_mode": "single_mode",
            },
            requested_by_person_id=None,
            requested_by_vendor_id=None,
        )
        with pytest.raises(HTTPException, match="explicit assembly and lane model"):
            fiber_change_requests.approve_request(
                db_session,
                str(unsupported_multifiber_panel.id),
                reviewer_person_id=str(subscriber.id),
                review_notes="Multifiber panel lane contract checked",
            )

    port_id = _reviewed_inventory(
        db_session,
        subscriber,
        asset_type="fiber_connector_port",
        payload={
            "patch_panel_id": str(panel.id),
            "port_number": 12,
            "label": "ODF-12",
            "connector_type": "sc",
            "polish_type": "apc",
            "fiber_mode": "single_mode",
        },
    )
    assert db_session.get(FiberConnectorPort, port_id) is not None

    mismatched = fiber_change_requests.create_request(
        db_session,
        asset_type="fiber_connector_port",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload={
            "patch_panel_id": str(panel.id),
            "port_number": 11,
            "label": "ODF-11",
            "connector_type": "lc",
            "polish_type": "upc",
            "fiber_mode": "multi_mode",
        },
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    with pytest.raises(HTTPException, match="must match its panel"):
        fiber_change_requests.approve_request(
            db_session,
            str(mismatched.id),
            reviewer_person_id=str(subscriber.id),
            review_notes="Connector compatibility checked",
        )

    for connector_type in ("mpo", "mtp"):
        unsupported_multifiber = fiber_change_requests.create_request(
            db_session,
            asset_type="fiber_connector_port",
            asset_id=None,
            operation=FiberChangeRequestOperation.create,
            payload={
                "patch_panel_id": str(panel.id),
                "port_number": 10 if connector_type == "mpo" else 9,
                "label": connector_type.upper(),
                "connector_type": connector_type,
                "polish_type": "apc",
                "fiber_mode": "single_mode",
            },
            requested_by_person_id=None,
            requested_by_vendor_id=None,
        )
        with pytest.raises(HTTPException, match="explicit assembly and lane model"):
            fiber_change_requests.approve_request(
                db_session,
                str(unsupported_multifiber.id),
                reviewer_person_id=str(subscriber.id),
                review_notes="Multifiber lane contract checked",
            )

    invalid = fiber_change_requests.create_request(
        db_session,
        asset_type="fiber_connector_port",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload={
            "patch_panel_id": str(panel.id),
            "port_number": 13,
            "label": "ODF-13",
            "connector_type": "sc",
            "polish_type": "apc",
            "fiber_mode": "single_mode",
        },
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    with pytest.raises(HTTPException, match="exceeds declared panel capacity"):
        fiber_change_requests.approve_request(
            db_session,
            str(invalid.id),
            reviewer_person_id=str(subscriber.id),
            review_notes="Capacity checked",
        )


def test_duplex_patch_assembly_is_two_explicit_optical_channels(
    db_session, subscriber, network_device
):
    site = db_session.get(PopSite, network_device.pop_site_id)
    assert site is not None
    rack_id = _reviewed_inventory(
        db_session,
        subscriber,
        asset_type="fiber_rack",
        payload={
            "code": f"RACK-{uuid.uuid4().hex[:10]}",
            "name": "Reviewed duplex patch rack",
            "pop_site_id": str(site.id),
            "rack_units": 12,
        },
    )
    panel_ids = [
        _reviewed_inventory(
            db_session,
            subscriber,
            asset_type="fiber_patch_panel",
            payload={
                "rack_id": str(rack_id),
                "name": f"LC duplex panel {side}",
                "panel_type": "patch_panel",
                "rack_unit_start": rack_unit,
                "rack_unit_height": 1,
                "port_capacity": 2,
                "connector_type": "lc",
                "polish_type": "upc",
                "fiber_mode": "single_mode",
            },
        )
        for side, rack_unit in (("A", 1), ("B", 2))
    ]
    connector_ids = [
        _reviewed_inventory(
            db_session,
            subscriber,
            asset_type="fiber_connector_port",
            payload={
                "patch_panel_id": str(panel_id),
                "port_number": port_number,
                "label": f"{side}-{port_number}",
                "connector_type": "lc",
                "polish_type": "upc",
                "fiber_mode": "single_mode",
            },
        )
        for side, panel_id in zip(("A", "B"), panel_ids, strict=True)
        for port_number in (1, 2)
    ]
    assembly_label = f"LC-DUPLEX-{uuid.uuid4().hex[:10]}"

    for channel, first_index, second_index in (("TX", 0, 2), ("RX", 1, 3)):
        _connect(
            db_session,
            "patch_cord",
            first_connector_port_id=connector_ids[first_index],
            second_connector_port_id=connector_ids[second_index],
            label=f"{assembly_label}-{channel}",
            assembly_label=assembly_label,
            length_m="2.0",
            insertion_loss_db="0.2",
        )

    channels = list(
        db_session.scalars(
            select(FiberPatchCord)
            .where(
                FiberPatchCord.assembly_label == assembly_label,
                FiberPatchCord.active.is_(True),
            )
            .order_by(FiberPatchCord.label)
        )
    )

    assert len(channels) == 2
    assert len({channel.created_by_decision_id for channel in channels}) == 2
    assert {
        connector_id
        for channel in channels
        for connector_id in (
            channel.first_connector_port_id,
            channel.second_connector_port_id,
        )
    } == set(connector_ids)


def test_subscription_core_trace_includes_rack_odf_patch_and_exact_cores(
    db_session,
    subscription,
    subscriber,
    olt_device,
    network_device,
):
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )
    physical = install_complete_core_path(db_session, assets, network_device)

    result = resolve_subscription_core_continuity(db_session, subscription)

    assert result.complete is True
    assert result.gaps == ()
    assert result.logical_segment_ids == (
        assets["feeder"].id,
        assets["drop"].id,
    )
    kinds = [hop.kind for hop in result.hops]
    assert "fiber_rack" in kinds
    assert "odf" in kinds
    assert "patch_port" in kinds
    assert "patch_cord" in kinds
    assert kinds.count("fiber_strand") == 2
    assert physical["rack"].id in {hop.asset_id for hop in result.hops}
    assert len(result.evidence_sha256) == 64

    odf_termination = db_session.scalar(
        select(FiberStrandTermination).where(
            FiberStrandTermination.connector_port_id == physical["odf_port"].id,
            FiberStrandTermination.active.is_(True),
        )
    )
    assert odf_termination is not None
    with pytest.raises(FiberPhysicalContinuityError, match="in-use core"):
        preview_physical_link(
            db_session,
            "strand_termination",
            "disconnect",
            target_link_id=odf_termination.id,
        )

    patch = db_session.get(
        FiberPatchCord,
        uuid.UUID(physical["patch_decision"].result_payload["link_id"]),
    )
    assert patch is not None
    with pytest.raises(FiberPhysicalContinuityError, match="in-use core"):
        preview_physical_link(
            db_session,
            "patch_cord",
            "disconnect",
            target_link_id=patch.id,
        )


def test_exact_core_splice_joins_cable_ends_only_at_the_declared_closure(
    db_session,
    subscription,
    subscriber,
    olt_device,
    network_device,
):
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )
    feeder = assets["feeder"]
    original_end = feeder.to_point
    assert original_end is not None
    closure = FiberSpliceClosure(
        name=f"Reviewed splice closure {uuid.uuid4().hex[:10]}",
        is_active=True,
    )
    db_session.add(closure)
    db_session.flush()
    closure_point = FiberTerminationPoint(
        name="Exact mid-span closure endpoint",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
        is_active=True,
    )
    db_session.add(closure_point)
    db_session.flush()
    feeder.to_point_id = closure_point.id
    continuation = FiberSegment(
        name=f"closure-continuation-{uuid.uuid4().hex}",
        from_point_id=closure_point.id,
        to_point_id=original_end.id,
        route_geom="LINESTRING(7.49 9.08, 7.50 9.09)",
        fiber_count=12,
        is_active=True,
    )
    db_session.add(continuation)
    db_session.flush()
    for segment in (feeder, continuation):
        ensure_segment_strand_inventory(db_session, segment)
    db_session.flush()
    first_core = db_session.scalar(
        select(FiberStrand).where(
            FiberStrand.segment_id == feeder.id,
            FiberStrand.strand_number == 1,
        )
    )
    second_core = db_session.scalar(
        select(FiberStrand).where(
            FiberStrand.segment_id == continuation.id,
            FiberStrand.strand_number == 1,
        )
    )
    assert first_core is not None and second_core is not None
    first_core.status = FiberStrandStatus.in_use
    second_core.status = FiberStrandStatus.in_use
    pon_connector = _connector(
        pon_port_id=assets["pon"].id,
        label="Exact PON connector",
    )
    splitter_connector = _connector(
        splitter_port_id=assets["input"].id,
        label="Exact splitter input connector",
    )
    db_session.add_all([pon_connector, splitter_connector])
    db_session.commit()

    _connect(
        db_session,
        "strand_termination",
        first_strand_id=first_core.id,
        first_strand_end="a",
        connector_port_id=pon_connector.id,
    )
    _connect(
        db_session,
        "core_splice",
        first_strand_id=first_core.id,
        first_strand_end="b",
        second_strand_id=second_core.id,
        second_strand_end="a",
        splice_closure_id=closure.id,
        position=1,
        splice_type="fusion",
        insertion_loss_db="0.1",
    )
    _connect(
        db_session,
        "strand_termination",
        first_strand_id=second_core.id,
        first_strand_end="b",
        connector_port_id=splitter_connector.id,
    )

    result = resolve_core_continuity(
        db_session,
        start_endpoint_type="pon_port",
        start_endpoint_id=assets["pon"].id,
        end_endpoint_type="splitter_port",
        end_endpoint_id=assets["input"].id,
        logical_segment_ids=(feeder.id, continuation.id),
    )

    assert result.complete is True
    assert result.logical_segment_ids == (feeder.id, continuation.id)
    assert "core_splice" in {hop.kind for hop in result.hops}
