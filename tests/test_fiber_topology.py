from app.models.catalog import SubscriptionStatus
from app.models.network import (
    FdhCabinet,
    FiberSegment,
    FiberTerminationPoint,
    ODNEndpointType,
    OntAssignment,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortType,
)
from app.services.fiber_topology import audit_fiber_topology
from app.services.network.fiber_plant_integrity import ensure_segment_strand_inventory


def test_audit_fails_closed_when_only_a_fiber_subscription_exists(
    db_session, subscription
):
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    report = audit_fiber_topology(db_session)

    assert report.electronic.active_fiber_subscriptions == 1
    assert report.electronic.exact_subscription_assignments == 0
    assert report.inventory.active_segments == 0
    assert report.customer_trace_evidence_complete is False
    assert {finding.code for finding in report.findings} >= {
        "fiber_subscription_without_exact_ont",
        "fiber_subscription_not_traceable_to_splitter",
        "passive_plant_inventory_empty",
        "passive_segment_graph_empty",
    }


def test_audit_accepts_one_fully_connected_customer_path(
    db_session,
    subscription,
    subscriber,
    olt_device,
    network_device,
):
    subscription.status = SubscriptionStatus.active
    network_device.matched_device_type = "olt"
    network_device.matched_device_id = olt_device.id

    pon = PonPort(olt_id=olt_device.id, name="0/1/0", is_active=True)
    fdh = FdhCabinet(
        name="FDH-001",
        code="FDH-001",
        latitude=9.08,
        longitude=7.49,
        is_active=True,
    )
    splitter = Splitter(name="SPL-001", fdh=fdh, splitter_ratio="1:8", is_active=True)
    splitter_input = SplitterPort(
        splitter=splitter,
        port_number=0,
        port_type=SplitterPortType.input,
        is_active=True,
    )
    splitter_output = SplitterPort(
        splitter=splitter,
        port_number=1,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    ont = OntUnit(
        serial_number="ONT-001",
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
        splitter_port_id=splitter_output.id,
        is_active=True,
    )
    db_session.add_all([pon, fdh, splitter, splitter_input, splitter_output])
    db_session.flush()
    ont.pon_port_id = pon.id
    ont.splitter_port_id = splitter_output.id
    db_session.add(ont)
    db_session.flush()
    db_session.add_all(
        [
            PonPortSplitterLink(
                pon_port_id=pon.id,
                splitter_port_id=splitter_input.id,
                active=True,
            ),
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon.id,
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                active=True,
            ),
        ]
    )
    upstream = FiberTerminationPoint(
        name="PON termination",
        endpoint_type=ODNEndpointType.pon_port,
        ref_id=pon.id,
        is_active=True,
    )
    downstream = FiberTerminationPoint(
        name="Splitter input termination",
        endpoint_type=ODNEndpointType.splitter_port,
        ref_id=splitter_input.id,
        is_active=True,
    )
    db_session.add_all([upstream, downstream])
    db_session.flush()
    segment = FiberSegment(
        name="SEG-001",
        from_point_id=upstream.id,
        to_point_id=downstream.id,
        route_geom="LINESTRING(7.48 9.07, 7.49 9.08)",
        fiber_count=12,
        is_active=True,
    )
    db_session.add(segment)
    db_session.flush()
    ensure_segment_strand_inventory(db_session, segment)
    db_session.commit()

    report = audit_fiber_topology(db_session)

    assert report.electronic.exact_subscription_assignments == 1
    assert report.electronic.subscriptions_traceable_to_splitter == 1
    assert report.passive.connected_segments_with_geometry == 1
    assert report.findings == ()
    assert report.aggregate_preconditions_ready is True
    assert report.trace_coverage is None
    assert report.customer_trace_evidence_complete is False


def test_audit_detects_ont_and_assignment_on_another_olts_pon(
    db_session, subscription, subscriber, olt_device
):
    from app.models.network import OLTDevice

    subscription.status = SubscriptionStatus.active
    other_olt = OLTDevice(name="Other OLT", is_active=True)
    db_session.add(other_olt)
    db_session.flush()
    other_pon = PonPort(olt_id=other_olt.id, name="0/1/1", is_active=True)
    db_session.add(other_pon)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-WRONG-OLT",
        olt_device_id=olt_device.id,
        pon_port_id=other_pon.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=other_pon.id,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            active=True,
        )
    )
    db_session.commit()

    report = audit_fiber_topology(db_session)

    assert report.electronic.onts_on_wrong_olt_pon == 1
    assert report.electronic.assignments_on_wrong_olt_pon == 1
    assert {finding.code for finding in report.findings} >= {
        "ont_pon_wrong_olt",
        "assignment_pon_wrong_olt",
    }
