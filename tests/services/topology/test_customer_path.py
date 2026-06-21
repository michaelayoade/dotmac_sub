"""resolve_customer_path: ONT -> access device -> basestation (Phase 1, Task 6)."""

from __future__ import annotations

from app.models.catalog import NasDevice
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.customer_path import (
    GAP_NO_NODE,
    GAP_NO_ONT,
    resolve_customer_path,
)


def _node(matched_type, device_id, pop_site_id, hostid):
    return NetworkDevice(
        name=f"{matched_type}-node-{hostid}",
        matched_device_type=matched_type,
        matched_device_id=device_id,
        pop_site_id=pop_site_id,
        zabbix_hostid=hostid,
    )


def test_fiber_happy_path(db_session, subscriber, subscription):
    olt = OLTDevice(name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1")
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(_node("olt", olt.id, pop.id, "201"))
    ont = OntUnit(serial_number="SN-123", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)
    assert path.gap is None
    assert path.access_device_kind == "olt"
    assert path.access_device.id == olt.id
    assert path.ont.id == ont.id
    assert path.basestation.id == pop.id


def test_non_fiber_happy_path(db_session, subscription):
    nas = NasDevice(name="NAS-1", management_ip="10.0.0.5")
    pop = PopSite(name="Lekki", zabbix_group_id="11")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "202"))
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)
    assert path.gap is None
    assert path.access_device_kind == "nas"
    assert path.access_device.id == nas.id
    assert path.ont is None
    assert path.basestation.id == pop.id


def test_gap_no_ont_when_no_provisioning(db_session, subscription):
    # No ONT assignment and no provisioning NAS -> provisioning incomplete.
    path = resolve_customer_path(db_session, subscription)
    assert path.gap == GAP_NO_ONT
    assert path.access_device is None
    assert path.basestation is None


def test_gap_no_node_when_device_unmatched(db_session, subscriber, subscription):
    # Fiber ONT + OLT exist, but no NetworkDevice is matched to the OLT.
    olt = OLTDevice(name="OLT-2", hostname="olt2", mgmt_ip="10.0.0.2")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(serial_number="SN-456", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)
    assert path.gap == GAP_NO_NODE
    assert path.access_device.id == olt.id
    assert path.node is None
    assert path.basestation is None
