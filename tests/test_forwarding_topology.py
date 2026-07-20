from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.catalog import NasDevice
from app.models.forwarding_topology import (
    ForwardingControlObservation,
    ForwardingTopologyDecision,
    ForwardingTopologyDeclaration,
)
from app.models.network_monitoring import (
    DeviceInterface,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.services.network.forwarding_topology import (
    ForwardingTopologyError,
    execute_forwarding_topology_decision,
    inspect_forwarding_topology_decision,
    preview_forwarding_topology_decision,
    project_authoritative_forwarding_graph,
    propose_forwarding_topology_decision,
    reconcile_forwarding_topology,
    record_forwarding_control_observation,
    resolve_authoritative_upstream_chain,
    review_forwarding_topology_decision,
)

NOW = datetime.now(UTC)
EVIDENCE_SHA = hashlib.sha256(b"normalized collector evidence").hexdigest()


def _device(db, name: str, *, site: PopSite | None = None):
    site = site or PopSite(name=f"{name} site")
    device = NetworkDevice(name=name, pop_site=site, is_active=True)
    interface = DeviceInterface(device=device, name=f"{name}-uplink")
    db.add_all([site, device, interface])
    db.flush()
    return site, device, interface


def _internal_payload(
    downstream,
    downstream_interface,
    downstream_site,
    upstream,
    upstream_interface,
    upstream_site,
    *,
    path_key: str,
    downstream_role: str = "access",
    upstream_role: str = "core",
):
    return {
        "configuration_intent_ref": f"intent:{path_key}",
        "configuration_owner": "network.control_plane_intent",
        "downstream_device_id": str(downstream.id),
        "downstream_interface_id": str(downstream_interface.id),
        "downstream_pop_site_id": str(downstream_site.id),
        "downstream_role": downstream_role,
        "path_key": path_key,
        "path_kind": "internal",
        "preference": 100,
        "upstream_device_id": str(upstream.id),
        "upstream_interface_id": str(upstream_interface.id),
        "upstream_pop_site_id": str(upstream_site.id),
        "upstream_role": upstream_role,
        "vrf_name": "main",
    }


def _apply_decision(db, *, action, declaration, path_key, reason="verified path"):
    args = {
        "action": action,
        "declaration": declaration,
        "path_key": path_key,
        "reason": reason,
        "proposed_by": "network:proposer",
    }
    preview = preview_forwarding_topology_decision(db, **args)
    decision = propose_forwarding_topology_decision(
        db,
        **args,
        expected_decision_sha256=preview.decision_sha256,
        commit=False,
    )
    review_forwarding_topology_decision(
        db,
        decision.id,
        action="approve",
        reviewed_by="network:reviewer",
        review_notes="matched the approved design and evidence",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )
    return execute_forwarding_topology_decision(
        db,
        decision.id,
        executed_by="network:executor",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )


def _lldp(db, downstream, downstream_interface, upstream, upstream_interface):
    row = NetworkTopologyLink(
        source_device_id=downstream.id,
        source_interface_id=downstream_interface.id,
        target_device_id=upstream.id,
        target_interface_id=upstream_interface.id,
        source="lldp_neighbor",
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def _control_observation(
    db,
    device,
    interface,
    *,
    source_type,
    peer_ip=None,
    peer_asn=None,
    route_prefix=None,
    next_hop_ip=None,
    client_ref=None,
):
    return record_forwarding_control_observation(
        db,
        client_ref=client_ref or uuid.uuid4(),
        source_type=source_type,
        collector="test:normalized-control-plane",
        collector_run_id="collector-run-1",
        device_id=device.id,
        interface_id=interface.id,
        vrf_name="main",
        peer_ip=peer_ip,
        peer_asn=peer_asn,
        route_prefix=route_prefix,
        next_hop_ip=next_hop_ip,
        source_evidence_sha256=EVIDENCE_SHA,
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        commit=False,
    )


def test_raw_lldp_cannot_create_official_forwarding_path(db_session):
    site = PopSite(name="Shared site")
    _, access, access_interface = _device(db_session, "Access", site=site)
    _, core, core_interface = _device(db_session, "Core", site=site)
    _lldp(db_session, access, access_interface, core, core_interface)

    graph = project_authoritative_forwarding_graph(db_session)

    assert graph.adjacency == {}
    assert graph.root_device_ids == frozenset()
    assert resolve_authoritative_upstream_chain(db_session, access.id) == []


def test_reviewed_internal_path_requires_exact_current_lldp(db_session):
    site = PopSite(name="Internal site")
    _, access, access_interface = _device(db_session, "Access", site=site)
    _, core, core_interface = _device(db_session, "Core", site=site)
    payload = _internal_payload(
        access,
        access_interface,
        site,
        core,
        core_interface,
        site,
        path_key="internal:access-core",
    )
    decision = _apply_decision(
        db_session,
        action="declare",
        declaration=payload,
        path_key="internal:access-core",
    )

    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    assert report.state_counts["missing_observation"] == 1
    assert project_authoritative_forwarding_graph(db_session).adjacency == {}

    exact = _lldp(db_session, access, access_interface, core, core_interface)
    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    graph = project_authoritative_forwarding_graph(db_session)
    assert report.state_counts["agreement"] == 1
    assert graph.upstream_by_downstream == {access.id: core.id}
    assert [
        row.id for row in resolve_authoritative_upstream_chain(db_session, access.id)
    ] == [core.id]
    inspection = inspect_forwarding_topology_decision(db_session, decision.id)
    assert inspection["result_valid"] is True

    rogue_site, rogue, rogue_interface = _device(db_session, "Rogue")
    assert rogue_site.id != site.id
    conflict = _lldp(db_session, access, access_interface, rogue, rogue_interface)
    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    assert report.state_counts["drift"] == 1
    assert report.declarations[0]["evidence"]["lldp"]["conflict_link_ids"] == [
        str(conflict.id)
    ]
    assert project_authoritative_forwarding_graph(db_session).adjacency == {}
    assert exact.id != conflict.id


def test_border_projection_requires_bgp_and_route_agreement(db_session):
    site, border, interface = _device(db_session, "Border")
    payload = {
        "configuration_intent_ref": "routeros-intent:border-peer-64520",
        "configuration_owner": "network.routeros_sot",
        "downstream_device_id": str(border.id),
        "downstream_interface_id": str(interface.id),
        "downstream_pop_site_id": str(site.id),
        "downstream_role": "border",
        "next_hop_ip": "192.0.2.1",
        "path_key": "border:64520:main",
        "path_kind": "border_peer",
        "peer_asn": 64520,
        "peer_ip": "192.0.2.2",
        "preference": 100,
        "route_prefix": "0.0.0.0/0",
        "vrf_name": "main",
    }
    _apply_decision(
        db_session,
        action="declare",
        declaration=payload,
        path_key="border:64520:main",
    )
    bgp_ref = uuid.uuid4()
    first = _control_observation(
        db_session,
        border,
        interface,
        source_type="bgp_peer",
        peer_ip="192.0.2.2",
        peer_asn=64520,
        client_ref=bgp_ref,
    )
    replay = _control_observation(
        db_session,
        border,
        interface,
        source_type="bgp_peer",
        peer_ip="192.0.2.2",
        peer_asn=64520,
        client_ref=bgp_ref,
    )
    assert replay.id == first.id
    assert (
        reconcile_forwarding_topology(db_session, as_of=NOW).state_counts[
            "missing_observation"
        ]
        == 1
    )

    _control_observation(
        db_session,
        border,
        interface,
        source_type="routing_table",
        route_prefix="0.0.0.0/0",
        next_hop_ip="192.0.2.1",
    )
    # Other peers and prefixes on the same interface are separate facts, not
    # conflicts with the exact declared peer/default route.
    _control_observation(
        db_session,
        border,
        interface,
        source_type="bgp_peer",
        peer_ip="198.51.100.2",
        peer_asn=64521,
    )
    _control_observation(
        db_session,
        border,
        interface,
        source_type="routing_table",
        route_prefix="10.0.0.0/8",
        next_hop_ip="192.0.2.9",
    )
    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    graph = project_authoritative_forwarding_graph(db_session)
    assert report.ready_for_operational_projection is True
    assert graph.root_device_ids == frozenset({border.id})
    assert graph.adjacency == {}


def test_nas_radius_sessions_are_context_not_path_authority(db_session):
    site = PopSite(name="NAS site")
    _, nas_node, nas_interface = _device(db_session, "NAS node", site=site)
    _, core, core_interface = _device(db_session, "Core", site=site)
    nas = NasDevice(
        name="NAS",
        management_ip="10.0.0.1",
        network_device_id=nas_node.id,
        pop_site_id=site.id,
        is_active=True,
    )
    db_session.add(nas)
    db_session.flush()
    payload = {
        **_internal_payload(
            nas_node,
            nas_interface,
            site,
            core,
            core_interface,
            site,
            path_key="nas:termination",
            downstream_role="nas",
            upstream_role="core",
        ),
        "nas_device_id": str(nas.id),
        "next_hop_ip": "10.0.0.254",
        "path_kind": "nas_termination",
        "route_prefix": "0.0.0.0/0",
    }
    _apply_decision(
        db_session,
        action="declare",
        declaration=payload,
        path_key="nas:termination",
    )
    _lldp(db_session, nas_node, nas_interface, core, core_interface)
    _control_observation(
        db_session,
        nas_node,
        nas_interface,
        source_type="routing_table",
        route_prefix="0.0.0.0/0",
        next_hop_ip="10.0.0.254",
    )

    report = reconcile_forwarding_topology(db_session, as_of=NOW)
    row = report.declarations[0]
    assert row["evidence_state"] == "agreement"
    assert row["evidence"]["radius_sessions"] == {
        "active_session_count": 0,
        "authority": "online_session_observation_only",
    }
    assert project_authoritative_forwarding_graph(
        db_session
    ).upstream_by_downstream == {nas_node.id: core.id}


def test_independent_review_and_retirement_preserve_exact_evidence(db_session):
    site = PopSite(name="Retirement site")
    _, access, access_interface = _device(db_session, "Access", site=site)
    _, core, core_interface = _device(db_session, "Core", site=site)
    path_key = "internal:retire-me"
    payload = _internal_payload(
        access,
        access_interface,
        site,
        core,
        core_interface,
        site,
        path_key=path_key,
    )
    preview = preview_forwarding_topology_decision(
        db_session,
        action="declare",
        declaration=payload,
        path_key=path_key,
        reason="review boundary",
        proposed_by="same-actor",
    )
    assert db_session.query(ForwardingTopologyDecision).count() == 0
    decision = propose_forwarding_topology_decision(
        db_session,
        action="declare",
        declaration=payload,
        path_key=path_key,
        reason="review boundary",
        proposed_by="same-actor",
        expected_decision_sha256=preview.decision_sha256,
        commit=False,
    )
    with pytest.raises(ForwardingTopologyError, match="proposer cannot review"):
        review_forwarding_topology_decision(
            db_session,
            decision.id,
            action="approve",
            reviewed_by="same-actor",
            review_notes="not independent",
            expected_decision_sha256=decision.decision_sha256,
            commit=False,
        )
    review_forwarding_topology_decision(
        db_session,
        decision.id,
        action="approve",
        reviewed_by="independent-reviewer",
        review_notes="approved exact path",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )
    execute_forwarding_topology_decision(
        db_session,
        decision.id,
        executed_by="executor",
        expected_decision_sha256=decision.decision_sha256,
        commit=False,
    )
    declaration = db_session.query(ForwardingTopologyDeclaration).one()
    original_sha = declaration.declaration_sha256

    retired = _apply_decision(
        db_session,
        action="retire",
        declaration=None,
        path_key=path_key,
        reason="configuration intent retired",
    )
    db_session.refresh(declaration)
    assert retired.status == "applied"
    assert declaration.active is False
    assert declaration.declaration_sha256 == original_sha
    assert reconcile_forwarding_topology(db_session).declaration_count == 0
    assert db_session.query(ForwardingControlObservation).count() == 0


def test_declaration_preview_rejects_forwarding_cycle(db_session):
    site = PopSite(name="Cycle site")
    _, access, access_interface = _device(db_session, "Access", site=site)
    _, aggregation, aggregation_interface = _device(
        db_session, "Aggregation", site=site
    )
    first = _internal_payload(
        access,
        access_interface,
        site,
        aggregation,
        aggregation_interface,
        site,
        path_key="cycle:access-aggregation",
        downstream_role="access",
        upstream_role="aggregation",
    )
    _apply_decision(
        db_session,
        action="declare",
        declaration=first,
        path_key="cycle:access-aggregation",
    )
    reverse = _internal_payload(
        aggregation,
        aggregation_interface,
        site,
        access,
        access_interface,
        site,
        path_key="cycle:aggregation-access",
        downstream_role="aggregation",
        upstream_role="access",
    )

    with pytest.raises(ForwardingTopologyError, match="forwarding_cycle"):
        preview_forwarding_topology_decision(
            db_session,
            action="declare",
            declaration=reverse,
            path_key="cycle:aggregation-access",
            reason="must remain acyclic",
            proposed_by="network:proposer",
        )
