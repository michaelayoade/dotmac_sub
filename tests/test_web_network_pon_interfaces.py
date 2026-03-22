from app.models.network import OLTDevice, PonPort
from app.models.network_monitoring import DeviceInterface, InterfaceStatus, NetworkDevice
from app.services.web_network_pon_interfaces import build_page_data, parse_pon_port_notes, save_alias


def test_build_page_data_merges_modeled_and_discovered_interfaces(db_session):
    olt = OLTDevice(name="OLT-A", mgmt_ip="198.51.100.10", is_active=True)
    db_session.add(olt)
    db_session.flush()

    modeled = PonPort(olt_id=olt.id, name="0/1/0", notes="Primary splitter\n[[alias:Main Street]]", is_active=True)
    db_session.add(modeled)

    monitor = NetworkDevice(name="OLT-A", mgmt_ip="198.51.100.10", is_active=True)
    db_session.add(monitor)
    db_session.flush()

    db_session.add_all([
        DeviceInterface(device_id=monitor.id, name="gpon 0/1/0", status=InterfaceStatus.up, description="Frame A"),
        DeviceInterface(device_id=monitor.id, name="gpon 0/1/1", status=InterfaceStatus.down, description="Frame B"),
    ])
    db_session.commit()

    page = build_page_data(db_session)

    assert page["stats"]["total"] == 2
    assert page["stats"]["up"] == 1
    assert page["stats"]["down"] == 1
    assert page["stats"]["aliased"] == 1

    rows = page["rows"]
    assert rows[0]["alias"] == "Main Street"
    assert rows[0]["kind"] == "modeled"
    assert rows[1]["kind"] == "discovered"
    assert rows[1]["name"] == "gpon 0/1/1"


def test_build_page_data_filters_by_status_and_search(db_session):
    olt = OLTDevice(name="OLT-B", mgmt_ip="198.51.100.11", is_active=True)
    db_session.add(olt)
    db_session.flush()

    db_session.add_all([
        PonPort(olt_id=olt.id, name="0/2/0", notes="[[alias:Alpha]]", is_active=True),
        PonPort(olt_id=olt.id, name="0/2/1", notes="[[alias:Beta]]", is_active=True),
    ])
    monitor = NetworkDevice(name="OLT-B", mgmt_ip="198.51.100.11", is_active=True)
    db_session.add(monitor)
    db_session.flush()
    db_session.add_all([
        DeviceInterface(device_id=monitor.id, name="gpon 0/2/0", status=InterfaceStatus.up),
        DeviceInterface(device_id=monitor.id, name="gpon 0/2/1", status=InterfaceStatus.down),
    ])
    db_session.commit()

    filtered = build_page_data(db_session, status="down", search="beta")

    assert filtered["stats"]["total"] == 1
    assert filtered["rows"][0]["alias"] == "Beta"
    assert filtered["rows"][0]["status"] == "down"


def test_save_alias_updates_existing_pon_port(db_session):
    olt = OLTDevice(name="OLT-C", mgmt_ip="198.51.100.12", is_active=True)
    db_session.add(olt)
    db_session.flush()
    port = PonPort(olt_id=olt.id, name="0/3/0", is_active=True)
    db_session.add(port)
    db_session.commit()

    saved = save_alias(
        db_session,
        olt_id=str(olt.id),
        interface_name="0/3/0",
        alias="Campus Block",
        pon_port_id=str(port.id),
    )

    assert saved.id == port.id
    alias, cleaned = parse_pon_port_notes(saved.notes)
    assert alias == "Campus Block"
    assert cleaned is None


def test_save_alias_creates_modeled_pon_port_for_discovered_interface(db_session):
    olt = OLTDevice(name="OLT-D", mgmt_ip="198.51.100.13", is_active=True)
    db_session.add(olt)
    db_session.commit()

    saved = save_alias(
        db_session,
        olt_id=str(olt.id),
        interface_name="gpon 0/4/0",
        alias="Warehouse",
        pon_port_id=None,
    )

    assert saved.name == "gpon 0/4/0"
    alias, _cleaned = parse_pon_port_notes(saved.notes)
    assert alias == "Warehouse"
