"""resolve_customer_path: ONT -> access device -> basestation (Phase 1, Task 6)."""

from __future__ import annotations

from app.models.catalog import NasDevice
from app.models.network import (
    FdhCabinet,
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
)
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


def test_fiber_path_includes_physical_plant(db_session, subscriber, subscription):
    olt = OLTDevice(name="OLT-Plant", hostname="olt-plant", mgmt_ip="10.0.0.10")
    pop = PopSite(name="Gudu", zabbix_group_id="12")
    fdh = FdhCabinet(name="FDH Alpha", code="FDH-A")
    splitter = Splitter(name="SPL-A", fdh=fdh)
    db_session.add_all([olt, pop, fdh, splitter])
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/2")
    splitter_port = SplitterPort(splitter_id=splitter.id, port_number=8)
    db_session.add_all([pon, splitter_port])
    db_session.flush()
    db_session.add(_node("olt", olt.id, pop.id, "203"))
    ont = OntUnit(
        serial_number="SN-PLANT",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        splitter_port_id=splitter_port.id,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon.id,
        subscriber_id=subscriber.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.ont_assignment.id == assignment.id
    assert path.pon_port.id == pon.id
    assert path.splitter_port.id == splitter_port.id
    assert path.splitter.id == splitter.id
    assert path.fdh.id == fdh.id
    assert path.access_device.id == olt.id


def test_fiber_path_uses_assignment_and_pon_splitter_fallbacks(
    db_session, subscriber, subscription
):
    olt = OLTDevice(name="OLT-Fallback", hostname="olt-fallback", mgmt_ip="10.0.0.11")
    pop = PopSite(name="Jabi", zabbix_group_id="13")
    fdh = FdhCabinet(name="FDH Beta", code="FDH-B")
    splitter = Splitter(name="SPL-B", fdh=fdh)
    db_session.add_all([olt, pop, fdh, splitter])
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/3")
    splitter_port = SplitterPort(splitter_id=splitter.id, port_number=4)
    db_session.add_all([pon, splitter_port])
    db_session.flush()
    db_session.add_all(
        [
            _node("olt", olt.id, pop.id, "204"),
            PonPortSplitterLink(pon_port_id=pon.id, splitter_port_id=splitter_port.id),
        ]
    )
    ont = OntUnit(serial_number="SN-FALLBACK")
    db_session.add(ont)
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon.id,
                subscriber_id=subscriber.id,
                active=True,
            ),
            SplitterPortAssignment(
                splitter_port_id=splitter_port.id,
                subscriber_id=subscriber.id,
                active=True,
            ),
        ]
    )
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.pon_port.id == pon.id
    assert path.splitter_port.id == splitter_port.id
    assert path.splitter.id == splitter.id
    assert path.fdh.id == fdh.id
    assert path.access_device.id == olt.id


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


# --- Wireless arm: radio -> AP -> basestation (read-side of the UISP sync) ---


def _cpe(db, subscriber_id, parent_id, uisp_status="active", synced_at=None, **kw):
    from app.models.network import CPEDevice

    cpe = CPEDevice(
        subscriber_id=subscriber_id,
        parent_network_device_id=parent_id,
        last_uisp_status=uisp_status,
        uisp_synced_at=synced_at,
        **kw,
    )
    db.add(cpe)
    db.flush()
    return cpe


def _ap_node(db, name, pop_site_id=None, **kw):
    node = NetworkDevice(
        name=name, pop_site_id=pop_site_id, uisp_device_id=f"uisp-{name}", **kw
    )
    db.add(node)
    db.flush()
    return node


def test_wireless_happy_path_radio_ap_basestation(db_session, subscriber, subscription):
    from datetime import UTC, datetime

    from app.models.network_monitoring import DeviceRole, NetworkTopologyLink

    pop = PopSite(name="Karu BTS", zabbix_group_id="20")
    db_session.add(pop)
    db_session.flush()
    ap = _ap_node(db_session, "AP-Karu-Sector1", pop_site_id=pop.id)
    core = NetworkDevice(name="Core-1", role=DeviceRole.core, is_active=True)
    db_session.add(core)
    db_session.flush()
    db_session.add(
        NetworkTopologyLink(
            source_device_id=ap.id,
            target_device_id=core.id,
            source="lldp_neighbor",
            is_active=True,
        )
    )
    cpe = _cpe(
        db_session,
        subscriber.id,
        ap.id,
        synced_at=datetime(2026, 7, 1, tzinfo=UTC),
        model="LiteBeam 5AC",
    )

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.access_device_kind == "ap"
    assert path.radio.id == cpe.id
    assert path.access_device.id == ap.id
    assert path.node.id == ap.id
    assert path.basestation.id == pop.id
    assert [hop.id for hop in path.upstream_chain] == [core.id]


def test_wireless_beats_nas_fallback(db_session, subscriber, subscription):
    # A wireless subscriber whose PPPoE terminates on a NAS at the BTS: the
    # radio arm must win (finer: it pins the customer to the actual AP).
    nas = NasDevice(name="NAS-BTS", management_ip="10.0.0.9")
    nas_pop = PopSite(name="NAS Site", zabbix_group_id="21")
    ap_pop = PopSite(name="AP Site", zabbix_group_id="22")
    db_session.add_all([nas, nas_pop, ap_pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, nas_pop.id, "301"))
    ap = _ap_node(db_session, "AP-Fine", pop_site_id=ap_pop.id)
    _cpe(db_session, subscriber.id, ap.id)
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "ap"
    assert path.node.id == ap.id
    assert path.basestation.id == ap_pop.id


def test_fiber_precedence_over_wireless(db_session, subscriber, subscription):
    # An active ONT assignment implies fiber; the radio must not preempt it.
    olt = OLTDevice(name="OLT-W", hostname="olt-w", mgmt_ip="10.0.0.3")
    pop = PopSite(name="Fiber Site", zabbix_group_id="23")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(_node("olt", olt.id, pop.id, "302"))
    ont = OntUnit(serial_number="SN-W", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    ap = _ap_node(db_session, "AP-Ignored")
    _cpe(db_session, subscriber.id, ap.id)
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "olt"
    assert path.radio is None


def test_nas_fallback_when_cpe_has_no_parent(db_session, subscriber, subscription):
    nas = NasDevice(name="NAS-F", management_ip="10.0.0.7")
    pop = PopSite(name="Fallback Site", zabbix_group_id="24")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "303"))
    _cpe(db_session, subscriber.id, None)  # CPE exists but no AP edge
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "nas"
    assert path.radio is None
    assert path.basestation.id == pop.id


def test_nas_fallback_when_cpe_vanished(db_session, subscriber, subscription):
    nas = NasDevice(name="NAS-V", management_ip="10.0.0.8")
    pop = PopSite(name="Vanish Site", zabbix_group_id="25")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "304"))
    ap = _ap_node(db_session, "AP-Gone")
    _cpe(db_session, subscriber.id, ap.id, uisp_status="vanished")
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "nas"
    assert path.radio is None


def test_multiple_radios_pick_most_recently_synced(
    db_session, subscriber, subscription
):
    from datetime import UTC, datetime

    pop = PopSite(name="Multi Site", zabbix_group_id="26")
    db_session.add(pop)
    db_session.flush()
    ap_old = _ap_node(db_session, "AP-Old", pop_site_id=pop.id)
    ap_new = _ap_node(db_session, "AP-New", pop_site_id=pop.id)
    _cpe(
        db_session,
        subscriber.id,
        ap_old.id,
        synced_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    newer = _cpe(
        db_session,
        subscriber.id,
        ap_new.id,
        synced_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    path = resolve_customer_path(db_session, subscription)

    assert path.radio.id == newer.id
    assert path.node.id == ap_new.id
