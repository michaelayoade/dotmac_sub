"""affected_customers reverse traversal (Phase 4a, P4.1)."""

from __future__ import annotations

import uuid

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import (
    FdhCabinet,
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
)
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.models.subscriber import Address, Subscriber
from app.services.topology.affected import (
    affected_customers,
    fdh_impact_branches,
    fdh_impact_rows,
    impact_breakdown,
)


def _node(db, name, mtype=None, mid=None, pop_site_id=None, role=DeviceRole.edge):
    n = NetworkDevice(
        name=name,
        matched_device_type=mtype,
        matched_device_id=mid,
        pop_site_id=pop_site_id,
        role=role,
        is_active=True,
    )
    db.add(n)
    db.flush()
    return n


def _sub(
    db, offer_id, nas_id=None, status=SubscriptionStatus.active, subscriber_id=None
):
    if subscriber_id is None:
        s = Subscriber(
            first_name="A", last_name="B", email=f"{uuid.uuid4().hex}@ex.com"
        )
        db.add(s)
        db.flush()
        subscriber_id = s.id
    sub = Subscription(
        subscriber_id=subscriber_id,
        offer_id=offer_id,
        status=status,
        provisioning_nas_device_id=nas_id,
    )
    db.add(sub)
    db.flush()
    return sub


def _link(db, a, b):
    db.add(
        NetworkTopologyLink(
            source_device_id=a.id,
            target_device_id=b.id,
            source="lldp_neighbor",
            is_active=True,
        )
    )
    db.flush()


def test_nas_node_affected(db_session, catalog_offer):
    nas = NasDevice(name="NAS-1", management_ip="10.0.0.1")
    other = NasDevice(name="NAS-2", management_ip="10.0.0.2")
    db_session.add_all([nas, other])
    db_session.flush()
    node = _node(db_session, "nas1-node", "nas", nas.id)
    _sub(db_session, catalog_offer.id, nas_id=nas.id)
    _sub(db_session, catalog_offer.id, nas_id=nas.id)
    _sub(
        db_session, catalog_offer.id, nas_id=nas.id, status=SubscriptionStatus.canceled
    )  # excluded
    _sub(db_session, catalog_offer.id, nas_id=other.id)  # different NAS, excluded

    out = affected_customers(db_session, node=node)
    assert out["count"] == 2


def test_olt_node_affected_via_ont(db_session, subscriber, catalog_offer):
    olt = OLTDevice(name="OLT-1", hostname="o1", mgmt_ip="10.0.0.5")
    db_session.add(olt)
    db_session.flush()
    node = _node(db_session, "olt-node", "olt", olt.id)
    ont = OntUnit(serial_number="SN-1", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    _sub(db_session, catalog_offer.id, subscriber_id=subscriber.id)
    db_session.flush()

    out = affected_customers(db_session, node=node)
    assert out["count"] == 1


def test_basestation_aggregates_its_nodes(db_session, catalog_offer):
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add(pop)
    db_session.flush()
    nas_a = NasDevice(name="A", management_ip="10.0.1.1")
    nas_b = NasDevice(name="B", management_ip="10.0.1.2")
    db_session.add_all([nas_a, nas_b])
    db_session.flush()
    _node(db_session, "a", "nas", nas_a.id, pop_site_id=pop.id)
    _node(db_session, "b", "nas", nas_b.id, pop_site_id=pop.id)
    _sub(db_session, catalog_offer.id, nas_id=nas_a.id)
    _sub(db_session, catalog_offer.id, nas_id=nas_b.id)

    out = affected_customers(db_session, basestation=pop)
    assert out["count"] == 2


def test_fdh_affected_via_splitter_port_assignments(db_session, catalog_offer):
    fdh = FdhCabinet(name="FDH Alpha", code="FDH-A")
    other_fdh = FdhCabinet(name="FDH Beta", code="FDH-B")
    db_session.add_all([fdh, other_fdh])
    db_session.flush()
    splitter = Splitter(name="SPL-A", fdh_id=fdh.id)
    other_splitter = Splitter(name="SPL-B", fdh_id=other_fdh.id)
    db_session.add_all([splitter, other_splitter])
    db_session.flush()
    ports = [
        SplitterPort(splitter_id=splitter.id, port_number=1),
        SplitterPort(splitter_id=splitter.id, port_number=2),
        SplitterPort(splitter_id=other_splitter.id, port_number=1),
    ]
    db_session.add_all(ports)
    db_session.flush()

    sub_a = _sub(db_session, catalog_offer.id)
    sub_b = _sub(db_session, catalog_offer.id)
    unrelated = _sub(db_session, catalog_offer.id)  # unrelated, excluded
    db_session.add_all(
        [
            SplitterPortAssignment(
                splitter_port_id=ports[0].id,
                subscriber_id=sub_a.subscriber_id,
                active=True,
            ),
            SplitterPortAssignment(
                splitter_port_id=ports[1].id,
                subscriber_id=sub_b.subscriber_id,
                active=True,
            ),
            SplitterPortAssignment(
                splitter_port_id=ports[2].id,
                subscriber_id=unrelated.subscriber_id,
                active=True,
            ),
        ]
    )
    db_session.flush()

    out = affected_customers(db_session, fdh=fdh)
    assert out["count"] == 2
    assert {sub.id for sub in out["subscriptions"]} == {sub_a.id, sub_b.id}


def test_fdh_affected_via_direct_ont_splitter_reference(
    db_session, subscriber, catalog_offer
):
    fdh = FdhCabinet(name="FDH Alpha", code="FDH-A")
    splitter = Splitter(name="SPL-A", fdh=fdh)
    db_session.add_all([fdh, splitter])
    db_session.flush()
    ont = OntUnit(serial_number="SN-FDH", splitter_id=splitter.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    sub = _sub(db_session, catalog_offer.id, subscriber_id=subscriber.id)
    db_session.flush()

    out = affected_customers(db_session, fdh=fdh)
    assert out["count"] == 1
    assert out["subscriptions"][0].id == sub.id


def test_fdh_impact_rows_include_customer_and_plant_details(
    db_session, subscriber, catalog_offer
):
    fdh = FdhCabinet(name="FDH Alpha", code="FDH-A")
    splitter = Splitter(name="SPL-A", fdh=fdh)
    olt = OLTDevice(name="OLT Alpha", hostname="olt-alpha", mgmt_ip="10.0.0.8")
    db_session.add_all([fdh, splitter, olt])
    db_session.flush()
    port = SplitterPort(splitter_id=splitter.id, port_number=7)
    pon = PonPort(olt_id=olt.id, name="0/1/2")
    db_session.add_all([port, pon])
    db_session.flush()
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="12 Fiber Close",
        city="Abuja",
        region="FCT",
    )
    ont = OntUnit(
        serial_number="SN-FDH-DETAIL",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        splitter_port_id=port.id,
        olt_status=OnuOnlineStatus.online,
        onu_rx_signal_dbm=-24.2,
        olt_rx_signal_dbm=-23.8,
    )
    db_session.add_all([address, ont])
    db_session.flush()
    sub = _sub(db_session, catalog_offer.id, subscriber_id=subscriber.id)
    sub.service_address_id = address.id
    subscriber.phone = "08030000000"
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon.id,
                subscriber_id=subscriber.id,
                service_address_id=address.id,
                active=True,
            ),
            SplitterPortAssignment(
                splitter_port_id=port.id,
                subscriber_id=subscriber.id,
                service_address_id=address.id,
                active=True,
            ),
        ]
    )
    db_session.flush()

    rows = fdh_impact_rows(db_session, fdh)

    assert len(rows) == 1
    row = rows[0]
    assert row["subscription_id"] == sub.id
    assert row["phone"] == "08030000000"
    assert row["service_address"] == "12 Fiber Close, Abuja, FCT"
    assert row["ont_serial"] == "SN-FDH-DETAIL"
    assert row["olt_name"] == "OLT Alpha"
    assert row["pon_port_name"] == "0/1/2"
    assert row["splitter_name"] == "SPL-A"
    assert row["splitter_port_number"] == 7
    assert row["signal_status"] == "online"
    assert row["signal_quality"] == "good"


def test_upstream_node_captures_downstream(db_session, catalog_offer):
    # core - agg - access(nas); outage on agg should capture access's subs
    core = _node(db_session, "Core", role=DeviceRole.core)
    agg = _node(db_session, "Agg", role=DeviceRole.aggregation)
    nas = NasDevice(name="AccessNAS", management_ip="10.0.2.1")
    db_session.add(nas)
    db_session.flush()
    access = _node(db_session, "Access", "nas", nas.id, role=DeviceRole.access)
    _link(db_session, core, agg)
    _link(db_session, agg, access)
    _sub(db_session, catalog_offer.id, nas_id=nas.id)

    out = affected_customers(db_session, node=agg)
    assert out["count"] == 1  # reached the access NAS via the graph (away from core)


def test_no_match_is_empty(db_session):
    node = _node(db_session, "orphan", "nas", uuid.uuid4())
    out = affected_customers(db_session, node=node)
    assert out["count"] == 0


# --- Wireless arm: AP node -> subscriptions via the CPE -> AP edge ---


def _cpe(db, subscriber_id, parent_id, uisp_status="active", **kw):
    from app.models.network import CPEDevice

    cpe = CPEDevice(
        subscriber_id=subscriber_id,
        parent_network_device_id=parent_id,
        last_uisp_status=uisp_status,
        **kw,
    )
    db.add(cpe)
    db.flush()
    return cpe


def test_ap_node_affected_via_cpe_edge(db_session, catalog_offer):
    ap = _node(db_session, "AP-Sector1")
    other_ap = _node(db_session, "AP-Sector2")

    sub_a = _sub(db_session, catalog_offer.id)
    sub_b = _sub(db_session, catalog_offer.id)
    inactive = _sub(db_session, catalog_offer.id, status=SubscriptionStatus.canceled)
    vanished = _sub(db_session, catalog_offer.id)
    elsewhere = _sub(db_session, catalog_offer.id)
    _cpe(db_session, sub_a.subscriber_id, ap.id)
    _cpe(db_session, sub_b.subscriber_id, ap.id, uisp_status="disconnected")
    _cpe(db_session, inactive.subscriber_id, ap.id)  # sub not active, excluded
    _cpe(db_session, vanished.subscriber_id, ap.id, uisp_status="vanished")  # excluded
    _cpe(db_session, elsewhere.subscriber_id, other_ap.id)  # other AP, excluded

    out = affected_customers(db_session, node=ap)
    assert out["count"] == 2
    assert {s.id for s in out["subscriptions"]} == {sub_a.id, sub_b.id}


def test_ap_node_also_matched_nas_unions_both_arms(db_session, catalog_offer):
    # A node can be Zabbix-matched as a NAS *and* be a UISP AP: the arms are
    # additive and dedupe on subscription id.
    nas = NasDevice(name="NAS-AP", management_ip="10.0.3.1")
    db_session.add(nas)
    db_session.flush()
    node = _node(db_session, "bts-router", "nas", nas.id)
    nas_sub = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    wireless_sub = _sub(db_session, catalog_offer.id)
    both_sub = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    _cpe(db_session, wireless_sub.subscriber_id, node.id)
    _cpe(db_session, both_sub.subscriber_id, node.id)  # in both arms, deduped

    out = affected_customers(db_session, node=node)
    assert out["count"] == 3
    assert {s.id for s in out["subscriptions"]} == {
        nas_sub.id,
        wireless_sub.id,
        both_sub.id,
    }


def test_ap_node_excludes_retired_cpe(db_session, catalog_offer):
    from app.models.network import DeviceStatus

    ap = _node(db_session, "AP-Retire")
    live = _sub(db_session, catalog_offer.id)
    retired = _sub(db_session, catalog_offer.id)
    _cpe(db_session, live.subscriber_id, ap.id)
    _cpe(db_session, retired.subscriber_id, ap.id, status=DeviceStatus.retired)

    out = affected_customers(db_session, node=ap)
    assert out["count"] == 1
    assert out["subscriptions"][0].id == live.id


def test_subscriptions_for_nodes_matches_per_node_results(
    db_session, subscriber, catalog_offer
):
    # Batched resolver must agree with the single-node path on a mixed
    # nas/olt/ap node set (including a node with no subscribers).
    from app.services.topology.affected import (
        subscriptions_for_node,
        subscriptions_for_nodes,
    )

    nas = NasDevice(name="NAS-Mix", management_ip="10.0.4.1")
    olt = OLTDevice(name="OLT-Mix", hostname="olt-mix", mgmt_ip="10.0.4.2")
    db_session.add_all([nas, olt])
    db_session.flush()
    nas_node = _node(db_session, "mix-nas", "nas", nas.id)
    olt_node = _node(db_session, "mix-olt", "olt", olt.id)
    ap_node = _node(db_session, "mix-ap")
    empty_node = _node(db_session, "mix-empty")

    _sub(db_session, catalog_offer.id, nas_id=nas.id)
    ont = OntUnit(serial_number="SN-MIX", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    _sub(db_session, catalog_offer.id, subscriber_id=subscriber.id)
    wireless_sub = _sub(db_session, catalog_offer.id, nas_id=nas.id)  # in both arms
    _cpe(db_session, wireless_sub.subscriber_id, ap_node.id)
    db_session.flush()

    nodes = [nas_node, olt_node, ap_node, empty_node]
    batched = subscriptions_for_nodes(db_session, [n.id for n in nodes])

    assert set(batched) == {n.id for n in nodes}
    for node in nodes:
        assert {s.id for s in batched[node.id]} == {
            s.id for s in subscriptions_for_node(db_session, node)
        }
    assert batched[empty_node.id] == []


def test_impact_breakdown_ranks_and_scales_branches(db_session, catalog_offer):
    pop = PopSite(name="Kubwa", zabbix_group_id="20")
    db_session.add(pop)
    db_session.flush()
    nas_a = NasDevice(name="AA", management_ip="10.0.2.1")
    nas_b = NasDevice(name="BB", management_ip="10.0.2.2")
    nas_c = NasDevice(name="CC", management_ip="10.0.2.3")
    db_session.add_all([nas_a, nas_b, nas_c])
    db_session.flush()
    node_a = _node(
        db_session,
        "node-a",
        "nas",
        nas_a.id,
        pop_site_id=pop.id,
        role=DeviceRole.access,
    )
    _node(
        db_session,
        "node-b",
        "nas",
        nas_b.id,
        pop_site_id=pop.id,
        role=DeviceRole.access,
    )
    _node(
        db_session, "node-c", "nas", nas_c.id, pop_site_id=pop.id
    )  # 0 subs -> dropped
    node_a.live_status = "down"
    db_session.flush()
    _sub(db_session, catalog_offer.id, nas_id=nas_a.id)
    _sub(db_session, catalog_offer.id, nas_id=nas_a.id)
    _sub(db_session, catalog_offer.id, nas_id=nas_b.id)

    result = affected_customers(db_session, basestation=pop)
    branches = impact_breakdown(db_session, result)

    # zero-count node-c dropped; sorted by count desc
    assert [b["name"] for b in branches] == ["node-a", "node-b"]
    assert branches[0]["count"] == 2 and branches[0]["pct"] == 100
    assert branches[0]["live_status"] == "down"
    assert branches[0]["role"] == "access"
    assert branches[1]["count"] == 1 and branches[1]["pct"] == 50


# --- Live-session arm: RadiusActiveSession.nas_device_id (who's online now) ---


def _session(db, subscription, nas_device_id, subscriber_id=None):
    from datetime import UTC, datetime

    from app.models.radius_active_session import RadiusActiveSession

    ras = RadiusActiveSession(
        subscriber_id=(
            subscriber_id
            if subscriber_id is not None
            else getattr(subscription, "subscriber_id", None)
        ),
        subscription_id=getattr(subscription, "id", None),
        nas_device_id=nas_device_id,
        username="u",
        acct_session_id=uuid.uuid4().hex,
        session_start=datetime.now(UTC),
    )
    db.add(ras)
    db.flush()
    return ras


def test_live_session_arm_unions_with_provisioning_and_dedupes(
    db_session, catalog_offer
):
    # A NAS node: the live-session arm adds who is CONNECTED there now (roaming/
    # failover) on top of who is provisioned there, deduped by subscription id.
    nas = NasDevice(name="NAS-Live", management_ip="10.0.5.1")
    other = NasDevice(name="NAS-Other", management_ip="10.0.5.2")
    db_session.add_all([nas, other])
    db_session.flush()
    node = _node(db_session, "live-nas", "nas", nas.id)

    prov_only = _sub(db_session, catalog_offer.id, nas_id=nas.id)  # provisioned here
    live_only = _sub(db_session, catalog_offer.id, nas_id=other.id)  # live here only
    both = _sub(db_session, catalog_offer.id, nas_id=nas.id)  # both arms -> deduped
    elsewhere = _sub(db_session, catalog_offer.id, nas_id=other.id)  # live on other
    canceled = _sub(
        db_session,
        catalog_offer.id,
        nas_id=other.id,
        status=SubscriptionStatus.canceled,
    )  # live here but inactive subscription -> excluded

    _session(db_session, live_only, nas.id)
    _session(db_session, both, nas.id)  # also in provisioning arm
    _session(db_session, elsewhere, other.id)  # session on a different NAS
    _session(db_session, canceled, nas.id)

    out = affected_customers(db_session, node=node)
    assert out["count"] == 3
    assert {s.id for s in out["subscriptions"]} == {
        prov_only.id,
        live_only.id,
        both.id,
    }


def test_subscriptions_for_nodes_live_session_parity(db_session, catalog_offer):
    # Batched must agree with the single-node path once the live-session arm
    # contributes (a node with a live session + an empty node).
    from app.services.topology.affected import (
        subscriptions_for_node,
        subscriptions_for_nodes,
    )

    nas = NasDevice(name="NAS-P", management_ip="10.0.6.1")
    other = NasDevice(name="NAS-Q", management_ip="10.0.6.2")
    db_session.add_all([nas, other])
    db_session.flush()
    node = _node(db_session, "parity-nas", "nas", nas.id)
    empty = _node(db_session, "parity-empty", "nas", other.id)

    _sub(db_session, catalog_offer.id, nas_id=nas.id)  # provisioning arm
    live = _sub(db_session, catalog_offer.id, nas_id=other.id)  # live arm only
    _session(db_session, live, nas.id)

    nodes = [node, empty]
    batched = subscriptions_for_nodes(db_session, [n.id for n in nodes])
    assert set(batched) == {n.id for n in nodes}
    for n in nodes:
        assert {s.id for s in batched[n.id]} == {
            s.id for s in subscriptions_for_node(db_session, n)
        }


def test_fdh_impact_branches_group_and_scale():
    rows = [
        {"splitter_name": "SPL-1"},
        {"splitter_name": "SPL-1"},
        {"splitter_name": "SPL-2"},
        {"splitter_name": None},
    ]
    branches = fdh_impact_branches(rows)
    by_name = {b["name"]: b for b in branches}
    assert by_name["SPL-1"]["count"] == 2 and by_name["SPL-1"]["pct"] == 100
    assert by_name["SPL-2"]["count"] == 1 and by_name["SPL-2"]["pct"] == 50
    assert by_name["—"]["count"] == 1  # None -> em dash bucket
    assert branches[0]["name"] == "SPL-1"  # busiest first
    assert all(b["live_status"] == "plant" for b in branches)
