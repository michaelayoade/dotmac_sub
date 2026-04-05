from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from app.models.event_store import EventStore
from app.models.network import (
    CPEDevice,
    ConfigMethod,
    DeviceStatus,
    IpProtocol,
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
    OntUnit,
    PonPort,
    WanMode,
)
from app.schemas.network import OntAssignmentCreate
from app.services import network as network_service
from app.services.web_network_ont_actions import return_to_inventory


def test_return_to_inventory_releases_ont_on_olt_and_marks_inventory_inactive(
    db_session, subscriber, monkeypatch
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

    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            account_id=subscriber.id,
            active=True,
        ),
    )

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

    assert ont.is_active is False
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
    cpe = db_session.scalars(
        select(CPEDevice).where(CPEDevice.serial_number == ont.serial_number).limit(1)
    ).first()
    assert cpe is not None
    assert cpe.status == DeviceStatus.inactive
    assert cpe.subscriber_id != subscriber.id
    assert cpe.service_address_id is None


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


def test_return_to_inventory_succeeds_with_ambiguous_cpe_serial_match(
    db_session, subscriber, monkeypatch
):
    olt = OLTDevice(name="OLT-Return-Ambiguous", mgmt_ip="198.51.100.60", is_active=True)
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/1", is_active=True)
    db_session.add(pon)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-AMB-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="17",
        provisioning_status=OntProvisioningStatus.provisioned,
    )
    db_session.add(ont)
    db_session.commit()

    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        ),
    )

    inventory_subscriber = network_service.cpe.get_inventory_subscriber(db_session)
    if inventory_subscriber is None:
        inventory_subscriber = network_service.cpe._get_or_create_inventory_subscriber(
            db_session
        )
        db_session.commit()

    duplicate_cpe = CPEDevice(
        subscriber_id=inventory_subscriber.id,
        serial_number=ont.serial_number,
        status=DeviceStatus.inactive,
    )
    db_session.add(duplicate_cpe)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "Found 0 service-port(s)", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda _olt, _fsp, _ont_id: (True, "ONT deleted"),
    )

    result = return_to_inventory(db_session, str(ont.id))

    assert result.success is True
    db_session.refresh(ont)
    db_session.refresh(assignment)
    assert ont.is_active is False
    assert assignment.active is False
    alert = db_session.scalars(
        select(EventStore)
        .where(EventStore.event_type == "network.alert")
        .order_by(EventStore.created_at.desc())
        .limit(1)
    ).first()
    assert alert is not None
    assert alert.payload["code"] == "ambiguous_ont_cpe_serial"
    assert alert.payload["ont_id"] == str(ont.id)
