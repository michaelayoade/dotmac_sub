"""affected_customers reverse traversal (Phase 4a, P4.1)."""

from __future__ import annotations

import uuid

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.models.subscriber import Subscriber
from app.services.topology.affected import affected_customers


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
