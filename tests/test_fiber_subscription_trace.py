from datetime import UTC, datetime
from decimal import Decimal

from fastapi.routing import APIRoute

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    FdhCabinet,
    FiberSegment,
    FiberSpliceClosure,
    FiberTerminationPoint,
    ODNEndpointType,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortType,
)
from app.services.fiber_topology import (
    audit_fiber_topology,
    localize_fiber_fault,
    trace_fiber_subscription,
)
from app.services.network.fiber_access_attachments import (
    approve_access_attachment,
    execute_access_attachment,
    propose_access_attachment,
)
from app.services.network.fiber_plant_integrity import ensure_segment_strand_inventory
from app.web.admin import network_fiber_plant as web_fiber_plant


def test_fiber_trace_admin_route_is_read_only_and_permission_guarded():
    route = next(
        route
        for route in web_fiber_plant.router.routes
        if isinstance(route, APIRoute)
        and route.path == "/network/fiber-trace"
        and "GET" in route.methods
    )
    captured = []
    for dependency in route.dependant.dependencies:
        for cell in getattr(dependency.call, "__closure__", None) or ():
            captured.append(cell.cell_contents)
    assert any("network:fiber:read" in str(value) for value in captured)
    assert not any(
        isinstance(route, APIRoute)
        and route.path == "/network/fiber-trace"
        and route.methods.intersection({"POST", "PUT", "PATCH", "DELETE"})
        for route in web_fiber_plant.router.routes
    )


def test_fiber_trace_template_compiles():
    assert (
        web_fiber_plant.templates.env.get_template("admin/network/fiber/trace.html")
        is not None
    )


def _add_segment(db_session, name, start_type, start_id, end_type, end_id):
    start = FiberTerminationPoint(
        name=f"{name} start",
        endpoint_type=start_type,
        ref_id=start_id,
        is_active=True,
    )
    end = FiberTerminationPoint(
        name=f"{name} end",
        endpoint_type=end_type,
        ref_id=end_id,
        is_active=True,
    )
    db_session.add_all([start, end])
    db_session.flush()
    segment = FiberSegment(
        name=name,
        from_point_id=start.id,
        to_point_id=end.id,
        route_geom="LINESTRING(7.48 9.07, 7.49 9.08)",
        fiber_count=12,
        is_active=True,
    )
    db_session.add(segment)
    db_session.flush()
    return start, end, segment


def _complete_path(
    db_session,
    subscription,
    subscriber,
    olt_device,
    network_device,
    *,
    seen_at=None,
    status=OnuOnlineStatus.online,
):
    subscription.status = SubscriptionStatus.active
    olt_device.is_active = True
    network_device.matched_device_type = "olt"
    network_device.matched_device_id = olt_device.id
    pon = PonPort(olt_id=olt_device.id, name="0/1/0", is_active=True)
    fdh = FdhCabinet(name="FDH 1", code="FDH-001", is_active=True)
    splitter = Splitter(
        name="Splitter 1", fdh=fdh, splitter_ratio="1:8", is_active=True
    )
    input_port = SplitterPort(
        splitter=splitter,
        port_number=0,
        port_type=SplitterPortType.input,
        is_active=True,
    )
    output_port = SplitterPort(
        splitter=splitter,
        port_number=1,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    db_session.add_all([pon, fdh, splitter, input_port, output_port])
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-TRACE-1",
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
        splitter_port_id=output_port.id,
        olt_status=status,
        olt_status_seen_at=seen_at,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add_all(
        [
            PonPortSplitterLink(
                pon_port_id=pon.id,
                splitter_port_id=input_port.id,
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
    feeder_start, feeder_end, feeder = _add_segment(
        db_session,
        "FEEDER-001",
        ODNEndpointType.pon_port,
        pon.id,
        ODNEndpointType.splitter_port,
        input_port.id,
    )
    _drop_start, _drop_end, drop = _add_segment(
        db_session,
        "DROP-001",
        ODNEndpointType.splitter_port,
        output_port.id,
        ODNEndpointType.ont,
        ont.id,
    )
    ensure_segment_strand_inventory(db_session, feeder)
    ensure_segment_strand_inventory(db_session, drop)
    db_session.commit()
    return {
        "pon": pon,
        "fdh": fdh,
        "splitter": splitter,
        "input": input_port,
        "output": output_port,
        "ont": ont,
        "feeder": feeder,
        "feeder_start": feeder_start,
        "feeder_end": feeder_end,
        "drop": drop,
    }


def _add_peer(
    db_session,
    *,
    subscriber,
    offer_id,
    olt_id,
    pon,
    splitter,
    port_number,
    status,
    seen_at,
):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
    )
    output = SplitterPort(
        splitter_id=splitter.id,
        port_number=port_number,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    db_session.add_all([subscription, output])
    db_session.flush()
    ont = OntUnit(
        serial_number=f"ONT-PEER-{port_number}",
        olt_device_id=olt_id,
        pon_port_id=pon.id,
        splitter_port_id=output.id,
        olt_status=status,
        olt_status_seen_at=seen_at,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            active=True,
        )
    )
    return subscription, ont


def test_trace_resolves_only_a_complete_explicit_customer_path(
    db_session, subscription, subscriber, olt_device, network_device
):
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )

    trace = trace_fiber_subscription(db_session, subscription.id)

    assert trace.customer_trace_complete is True
    assert trace.gaps == ()
    assert [hop.kind for hop in trace.hops] == [
        "pop",
        "olt",
        "pon_port",
        "termination",
        "feeder_segment",
        "termination",
        "fdh",
        "splitter",
        "splitter_input",
        "splitter_output",
        "termination",
        "drop_segment",
        "termination",
        "ont",
        "subscription",
        "customer",
    ]
    assert trace.hops[4].asset_id == assets["feeder"].id
    assert trace.upstream_scope == "pop_boundary_only"
    assert "LLDP adjacency alone" in trace.upstream_message

    audit = audit_fiber_topology(db_session, verify_customer_traces=True)
    assert audit.trace_coverage.exhaustive is True
    assert audit.trace_coverage.complete_traces == 1
    assert audit.customer_trace_evidence_complete is True
    assert audit.to_dict()["trace_coverage"]["coverage_ratio"] == 1.0


def test_trace_resolves_reviewed_cascade_stages_and_loss(
    db_session, subscription, subscriber, olt_device, network_device
):
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )
    assets["splitter"].insertion_loss_db = Decimal("3.500")
    cascade_output = SplitterPort(
        splitter=assets["splitter"],
        port_number=2,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    downstream_fdh = FdhCabinet(name="FDH 2", code="FDH-002", is_active=True)
    downstream_splitter = Splitter(
        name="Splitter 2",
        fdh=downstream_fdh,
        insertion_loss_db=Decimal("4.000"),
        splitter_ratio="1:8",
        is_active=True,
    )
    downstream_input = SplitterPort(
        splitter=downstream_splitter,
        port_number=0,
        port_type=SplitterPortType.input,
        is_active=True,
    )
    downstream_output = SplitterPort(
        splitter=downstream_splitter,
        port_number=1,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    db_session.add_all(
        [
            cascade_output,
            downstream_fdh,
            downstream_splitter,
            downstream_input,
            downstream_output,
        ]
    )
    db_session.flush()
    _add_segment(
        db_session,
        "DISTRIBUTION-001",
        ODNEndpointType.splitter_port,
        cascade_output.id,
        ODNEndpointType.splitter_port,
        downstream_input.id,
    )
    downstream_drop_start = FiberTerminationPoint(
        name="DROP-002 start",
        endpoint_type=ODNEndpointType.splitter_port,
        ref_id=downstream_output.id,
        is_active=True,
    )
    existing_ont_end = (
        db_session.query(FiberTerminationPoint)
        .filter(
            FiberTerminationPoint.endpoint_type == ODNEndpointType.ont,
            FiberTerminationPoint.ref_id == assets["ont"].id,
        )
        .one()
    )
    db_session.add(downstream_drop_start)
    db_session.flush()
    db_session.add(
        FiberSegment(
            name="DROP-002",
            from_point_id=downstream_drop_start.id,
            to_point_id=existing_ont_end.id,
            route_geom="LINESTRING(7.49 9.08, 7.50 9.09)",
            fiber_count=4,
            is_active=True,
        )
    )
    db_session.commit()

    cascade = propose_access_attachment(
        db_session,
        "splitter_cascade",
        "attach",
        cascade_output.id,
        downstream_input.id,
        proposed_by="planner@example.com",
        reason="Exact cascade ports and loss values independently verified",
    )
    approve_access_attachment(
        db_session,
        cascade.id,
        reviewed_by="reviewer@example.com",
        review_notes="Field labels and route evidence agree",
    )
    execute_access_attachment(
        db_session, cascade.id, executed_by="executor@example.com"
    )
    detach = propose_access_attachment(
        db_session,
        "ont_output",
        "detach",
        assets["ont"].id,
        proposed_by="planner@example.com",
        reason="Old drop endpoint removal verified",
    )
    approve_access_attachment(
        db_session,
        detach.id,
        reviewed_by="reviewer@example.com",
        review_notes="Old drop endpoint independently verified",
    )
    execute_access_attachment(db_session, detach.id, executed_by="executor@example.com")
    attach = propose_access_attachment(
        db_session,
        "ont_output",
        "attach",
        assets["ont"].id,
        downstream_output.id,
        proposed_by="planner@example.com",
        reason="Leaf splitter output and customer drop verified",
    )
    approve_access_attachment(
        db_session,
        attach.id,
        reviewed_by="reviewer@example.com",
        review_notes="Leaf output independently verified",
    )
    execute_access_attachment(db_session, attach.id, executed_by="executor@example.com")

    trace = trace_fiber_subscription(db_session, subscription.id)

    assert trace.customer_trace_complete is True
    assert trace.gaps == ()
    assert [hop.kind for hop in trace.hops] == [
        "pop",
        "olt",
        "pon_port",
        "termination",
        "feeder_segment",
        "termination",
        "fdh",
        "splitter",
        "splitter_input",
        "splitter_output",
        "termination",
        "distribution_segment",
        "termination",
        "splitter_cascade",
        "fdh",
        "splitter",
        "splitter_input",
        "splitter_output",
        "termination",
        "drop_segment",
        "termination",
        "ont",
        "subscription",
        "customer",
    ]
    splitter_hops = [hop for hop in trace.hops if hop.kind == "splitter"]
    assert [hop.splitter_stage for hop in splitter_hops] == [1, 2]
    assert [hop.insertion_loss_db for hop in splitter_hops] == ["3.500", "4.000"]
    assert splitter_hops[-1].cumulative_splitter_loss_db == "7.500"

    audit = audit_fiber_topology(db_session)
    assert audit.inventory.active_splitter_cascade_links == 1
    assert audit.passive.cascade_links_to_directed_ports == 1
    assert audit.passive.cascade_links_to_invalid_ports == 0
    assert audit.passive.cascade_links_in_cycles == 0
    assert audit.passive.cascade_port_role_conflicts == 0
    assert audit.passive.cascade_splitters_missing_loss == 0
    assert audit.passive.cascade_splitters_with_ambiguous_inputs == 0
    assert audit.passive.cascade_downstreams_with_multiple_upstreams == 0
    assert audit.passive.cascade_downstreams_with_pon_roots == 0
    assert audit.electronic.subscriptions_traceable_to_splitter == 1


def test_trace_does_not_promote_subscriber_assignment_fallback(
    db_session, subscription, subscriber, olt_device
):
    subscription.status = SubscriptionStatus.active
    ont = OntUnit(
        serial_number="ONT-FALLBACK-ONLY",
        olt_device_id=olt_device.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            subscriber_id=subscriber.id,
            subscription_id=None,
            active=True,
        )
    )
    db_session.commit()

    trace = trace_fiber_subscription(db_session, subscription.id)

    assert trace.electronic_complete is False
    assert trace.first_gap.code == "exact_ont_assignment_missing"
    assert "evidence only" in trace.first_gap.message


def test_trace_requires_review_when_physical_paths_are_ambiguous(
    db_session, subscription, subscriber, olt_device, network_device
):
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )
    db_session.add(
        FiberSegment(
            name="FEEDER-001-PARALLEL",
            from_point_id=assets["feeder_start"].id,
            to_point_id=assets["feeder_end"].id,
            route_geom="LINESTRING(7.48 9.0701, 7.49 9.0801)",
            fiber_count=12,
            is_active=True,
        )
    )
    db_session.commit()

    trace = trace_fiber_subscription(db_session, subscription.id)

    assert trace.electronic_complete is True
    assert trace.physical_complete is False
    assert {gap.code for gap in trace.gaps} == {"fiber_segment_path_ambiguous"}
    assert not any(hop.kind == "feeder_segment" for hop in trace.hops)
    assert any(hop.validation == "gap" for hop in trace.hops)
    assert trace.last_validated_scope.asset_id == assets["pon"].id


def test_fault_localization_ranks_shared_branch_without_naming_a_segment_failure(
    db_session, subscription, subscriber, olt_device, network_device
):
    now = datetime.now(UTC)
    assets = _complete_path(
        db_session,
        subscription,
        subscriber,
        olt_device,
        network_device,
        seen_at=now,
        status=OnuOnlineStatus.offline,
    )
    _add_peer(
        db_session,
        subscriber=subscriber,
        offer_id=subscription.offer_id,
        olt_id=olt_device.id,
        pon=assets["pon"],
        splitter=assets["splitter"],
        port_number=2,
        status=OnuOnlineStatus.offline,
        seen_at=now,
    )
    db_session.commit()

    result = localize_fiber_fault(db_session, subscription.id, now=now)

    assert result.telemetry_state == "offline"
    scopes = {candidate.scope for candidate in result.candidates}
    assert scopes == {"pon_shared_branch"}
    pon_candidate = next(
        candidate
        for candidate in result.candidates
        if candidate.scope == "pon_shared_branch"
    )
    assert pon_candidate.evidence.offline == 2
    assert assets["feeder"].id in pon_candidate.asset_ids
    assert assets["splitter"].id in pon_candidate.asset_ids
    assert "cannot select one segment" in pon_candidate.rationale


def test_fault_localization_ranks_customer_scope_when_a_peer_is_healthy(
    db_session, subscription, subscriber, olt_device, network_device
):
    now = datetime.now(UTC)
    assets = _complete_path(
        db_session,
        subscription,
        subscriber,
        olt_device,
        network_device,
        seen_at=now,
        status=OnuOnlineStatus.offline,
    )
    _add_peer(
        db_session,
        subscriber=subscriber,
        offer_id=subscription.offer_id,
        olt_id=olt_device.id,
        pon=assets["pon"],
        splitter=assets["splitter"],
        port_number=2,
        status=OnuOnlineStatus.online,
        seen_at=now,
    )
    db_session.commit()

    result = localize_fiber_fault(db_session, subscription.id, now=now)

    assert result.candidates[0].scope == "customer_drop_or_ont"
    assert result.candidates[0].confidence == "high"
    assert assets["drop"].id in result.candidates[0].asset_ids
    assert all(
        candidate.scope != "pon_shared_branch" for candidate in result.candidates
    )


def test_fault_localization_nominates_exact_shared_cable_for_field_verification(
    db_session, subscription, subscriber, olt_device, network_device
):
    now = datetime.now(UTC)
    assets = _complete_path(
        db_session,
        subscription,
        subscriber,
        olt_device,
        network_device,
        seen_at=now,
        status=OnuOnlineStatus.offline,
    )
    offline_subscription, offline_ont = _add_peer(
        db_session,
        subscriber=subscriber,
        offer_id=subscription.offer_id,
        olt_id=olt_device.id,
        pon=assets["pon"],
        splitter=assets["splitter"],
        port_number=2,
        status=OnuOnlineStatus.offline,
        seen_at=now,
    )
    online_subscription, online_ont = _add_peer(
        db_session,
        subscriber=subscriber,
        offer_id=subscription.offer_id,
        olt_id=olt_device.id,
        pon=assets["pon"],
        splitter=assets["splitter"],
        port_number=3,
        status=OnuOnlineStatus.online,
        seen_at=now,
    )
    del offline_subscription, online_subscription
    db_session.flush()
    offline_output = db_session.get(SplitterPort, offline_ont.splitter_port_id)
    online_output = db_session.get(SplitterPort, online_ont.splitter_port_id)
    assert offline_output is not None and online_output is not None

    assets["drop"].is_active = False
    closure_a = FiberSpliceClosure(name="Shared branch closure A", is_active=True)
    closure_b = FiberSpliceClosure(name="Shared branch closure B", is_active=True)
    db_session.add_all([closure_a, closure_b])
    db_session.flush()

    def termination(name, endpoint_type, ref_id):
        point = FiberTerminationPoint(
            name=name,
            endpoint_type=endpoint_type,
            ref_id=ref_id,
            is_active=True,
        )
        db_session.add(point)
        db_session.flush()
        return point

    selected_output_point = (
        db_session.query(FiberTerminationPoint)
        .filter(
            FiberTerminationPoint.endpoint_type == ODNEndpointType.splitter_port,
            FiberTerminationPoint.ref_id == assets["output"].id,
        )
        .one()
    )
    selected_ont_point = (
        db_session.query(FiberTerminationPoint)
        .filter(
            FiberTerminationPoint.endpoint_type == ODNEndpointType.ont,
            FiberTerminationPoint.ref_id == assets["ont"].id,
        )
        .one()
    )
    offline_output_point = termination(
        "Offline peer output",
        ODNEndpointType.splitter_port,
        offline_output.id,
    )
    online_output_point = termination(
        "Online peer output",
        ODNEndpointType.splitter_port,
        online_output.id,
    )
    offline_ont_point = termination(
        "Offline peer ONT", ODNEndpointType.ont, offline_ont.id
    )
    online_ont_point = termination(
        "Online peer ONT", ODNEndpointType.ont, online_ont.id
    )
    closure_a_point = termination(
        "Shared closure A", ODNEndpointType.splice_closure, closure_a.id
    )
    closure_b_point = termination(
        "Shared closure B", ODNEndpointType.splice_closure, closure_b.id
    )

    def segment(name, start, end):
        row = FiberSegment(
            name=name,
            from_point_id=start.id,
            to_point_id=end.id,
            route_geom="LINESTRING(7.48 9.07, 7.49 9.08)",
            fiber_count=12,
            is_active=True,
        )
        db_session.add(row)
        db_session.flush()
        return row

    segment("Selected branch ingress", selected_output_point, closure_a_point)
    segment("Offline peer branch ingress", offline_output_point, closure_a_point)
    shared = segment("Exact shared offline cable", closure_a_point, closure_b_point)
    segment("Selected branch egress", closure_b_point, selected_ont_point)
    segment("Offline peer branch egress", closure_b_point, offline_ont_point)
    segment("Online comparison drop", online_output_point, online_ont_point)
    db_session.commit()

    result = localize_fiber_fault(db_session, subscription.id, now=now)

    assert result.telemetry_state == "offline"
    assert result.candidates[0].scope == "shared_segment_candidate"
    assert result.candidates[0].asset_ids == (shared.id,)
    assert "field-verification candidate" in result.candidates[0].rationale


def test_fault_localization_refuses_to_rank_stale_telemetry(
    db_session, subscription, subscriber, olt_device, network_device
):
    assets = _complete_path(
        db_session,
        subscription,
        subscriber,
        olt_device,
        network_device,
        status=OnuOnlineStatus.offline,
    )

    result = localize_fiber_fault(db_session, subscription.id)

    assert result.trace.hops[-3].asset_id == assets["ont"].id
    assert result.telemetry_state == "stale"
    assert result.candidates == ()
    assert "no fault area was guessed" in result.telemetry_message
