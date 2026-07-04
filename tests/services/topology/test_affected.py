"""affected_customers reverse traversal (Phase 4a, P4.1)."""

from __future__ import annotations

import uuid

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import (
    FdhCabinet,
    OLTDevice,
    OnuOnlineStatus,
    OntAssignment,
    OntUnit,
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
from app.models.subscriber import Address
from app.models.subscriber import Subscriber
from app.services.topology.affected import affected_customers, fdh_impact_rows


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
