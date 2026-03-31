from __future__ import annotations

from types import SimpleNamespace

from app.models.network import (
    ConfigMethod,
    IpProtocol,
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
    OntUnit,
    PonPort,
    WanMode,
)
from app.services.web_network_ont_actions import return_to_inventory


def test_return_to_inventory_releases_ont_on_olt_and_keeps_inventory_record_active(
    db_session, monkeypatch
):
    olt = OLTDevice(name="OLT-Return", mgmt_ip="198.51.100.50", is_active=True)
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/1", is_active=True)
    db_session.add(pon)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="7",
        provisioning_status=OntProvisioningStatus.provisioned,
        wan_mode=WanMode.pppoe,
        config_method=ConfigMethod.tr069,
        ip_protocol=IpProtocol.dual_stack,
        pppoe_username="user1",
        pppoe_password="pass1",
        wan_remote_access=True,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        mgmt_ip_address="192.0.2.10",
        mgmt_remote_access=True,
        voip_enabled=True,
    )
    db_session.add(ont)
    db_session.commit()

    assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
    db_session.add(assignment)
    db_session.commit()

    deleted_indexes: list[int] = []

    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (
            True,
            "Found 2 service-port(s)",
            [SimpleNamespace(index=101), SimpleNamespace(index=202)],
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.delete_service_port",
        lambda _olt, index: (deleted_indexes.append(index) or True, "deleted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda _olt, _fsp, _ont_id: (True, "ONT deleted"),
    )

    result = return_to_inventory(db_session, str(ont.id))

    assert result.success is True
    assert "removed from OLT" in result.message
    assert deleted_indexes == [101, 202]

    db_session.refresh(ont)
    db_session.refresh(assignment)

    assert ont.is_active is True
    assert ont.external_id is None
    assert ont.provisioning_status == OntProvisioningStatus.unprovisioned
    assert ont.wan_mode is None
    assert ont.config_method is None
    assert ont.ip_protocol is None
    assert ont.pppoe_username is None
    assert ont.pppoe_password is None
    assert ont.wan_remote_access is False
    assert ont.mgmt_ip_mode is None
    assert ont.mgmt_ip_address is None
    assert ont.mgmt_remote_access is False
    assert ont.voip_enabled is False
    assert assignment.active is False


def test_return_to_inventory_keeps_local_state_when_olt_delete_fails(
    db_session, monkeypatch
):
    olt = OLTDevice(name="OLT-Return-Fail", mgmt_ip="198.51.100.51", is_active=True)
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/1", is_active=True)
    db_session.add(pon)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-FAIL-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="9",
        provisioning_status=OntProvisioningStatus.provisioned,
        pppoe_username="keepme",
    )
    db_session.add(ont)
    db_session.commit()

    assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
    db_session.add(assignment)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "Found 0 service-port(s)", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda _olt, _fsp, _ont_id: (False, "OLT rejected delete"),
    )

    result = return_to_inventory(db_session, str(ont.id))

    assert result.success is False
    assert "Failed to delete ONT from OLT" in result.message

    db_session.refresh(ont)
    db_session.refresh(assignment)

    assert ont.is_active is True
    assert ont.external_id == "9"
    assert ont.provisioning_status == OntProvisioningStatus.provisioned
    assert ont.pppoe_username == "keepme"
    assert assignment.active is True
