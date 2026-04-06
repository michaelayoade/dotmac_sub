"""Tests for TR-069 service."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.network import CPEDevice, OLTDevice, OntUnit
from app.models.tr069 import Tr069CpeDevice, Tr069Event, Tr069JobStatus
from app.schemas.network import OLTDeviceCreate, OntAssignmentCreate, OntUnitCreate, PonPortCreate
from app.schemas.tr069 import (
    Tr069AcsServerCreate,
    Tr069AcsServerUpdate,
    Tr069CpeDeviceCreate,
    Tr069CpeDeviceUpdate,
    Tr069JobCreate,
    Tr069JobUpdate,
    Tr069ParameterCreate,
    Tr069SessionCreate,
)
from app.services import network as network_service
from app.services import tr069 as tr069_service
from app.services import web_network_olts as web_network_olts_service
from app.services import web_network_tr069 as web_network_tr069_service
from app.services.genieacs import GenieACSError


def _acs_server_payload(**overrides) -> Tr069AcsServerCreate:
    data: dict[str, Any] = {
        "name": "GenieACS",
        "cwmp_url": "https://acs.example.com/cwmp",
        "cwmp_username": "acs-user",
        "cwmp_password": "acs-pass",
        "connection_request_username": "cr-user",
        "connection_request_password": "cr-pass",
        "base_url": "https://acs.example.com",
    }
    data.update(overrides)
    return Tr069AcsServerCreate.model_validate(data)


def test_create_acs_server(db_session):
    """Test creating an ACS server."""
    server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(),
    )
    assert server.name == "GenieACS"
    assert server.base_url == "https://acs.example.com"


def test_update_acs_server(db_session):
    """Test updating an ACS server."""
    server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(
            name="Original ACS",
            cwmp_url="https://old.acs.com/cwmp",
            base_url="https://old.acs.com",
        ),
    )
    updated = tr069_service.acs_servers.update(
        db_session,
        server.id,
        Tr069AcsServerUpdate(name="Updated ACS", base_url="https://new.acs.com"),
    )
    assert updated.name == "Updated ACS"
    assert updated.base_url == "https://new.acs.com"


def test_list_acs_servers(db_session):
    """Test listing ACS servers."""
    tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(
            name="ACS 1",
            cwmp_url="https://acs1.com/cwmp",
            base_url="https://acs1.com",
        ),
    )
    tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(
            name="ACS 2",
            cwmp_url="https://acs2.com/cwmp",
            base_url="https://acs2.com",
        ),
    )

    servers = tr069_service.acs_servers.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(servers) >= 2


def test_create_cpe_device(db_session, acs_server):
    """Test creating a TR-069 CPE device."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="CPE123456",
            oui="00259E",
            product_class="Router",
        ),
    )
    assert device.acs_server_id == acs_server.id
    assert device.serial_number == "CPE123456"


def test_create_cpe_device_rejects_missing_link_target(db_session, acs_server):
    with pytest.raises(HTTPException, match="CPE device not found") as exc_info:
        tr069_service.cpe_devices.create(
            db_session,
            Tr069CpeDeviceCreate(
                acs_server_id=acs_server.id,
                serial_number="CPE-CREATE-LINK-MISSING",
                cpe_device_id=uuid4(),
            ),
        )

    assert exc_info.value.status_code == 404


def test_create_cpe_device_rejects_parked_inventory_cpe(
    db_session, acs_server, subscriber
):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="TR069 Create OLT", hostname="tr069-create-olt.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/9"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="CREATE-PARKED-ONT"),
    )
    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            account_id=subscriber.id,
        ),
    )
    network_service.ont_assignments.delete(db_session, str(assignment.id))
    parked_cpe = db_session.scalars(
        select(CPEDevice)
        .where(CPEDevice.serial_number == "CREATE-PARKED-ONT")
        .limit(1)
    ).first()
    assert parked_cpe is not None

    with pytest.raises(
        HTTPException, match="Cannot link TR-069 device to parked inventory CPE"
    ) as exc_info:
        tr069_service.cpe_devices.create(
            db_session,
            Tr069CpeDeviceCreate(
                acs_server_id=acs_server.id,
                serial_number="CPE-CREATE-LINK-PARKED",
                cpe_device_id=parked_cpe.id,
            ),
        )

    assert exc_info.value.status_code == 400


def test_list_cpe_devices_by_server(db_session, acs_server):
    """Test listing CPE devices by ACS server."""
    tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="CPE001",
        ),
    )
    tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="CPE002",
        ),
    )

    devices = tr069_service.cpe_devices.list(
        db_session,
        acs_server_id=str(acs_server.id),
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(devices) >= 2
    assert all(d.acs_server_id == acs_server.id for d in devices)


def test_update_cpe_device(db_session, acs_server):
    """Test updating a CPE device."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="CPE999",
        ),
    )
    updated = tr069_service.cpe_devices.update(
        db_session,
        device.id,
        Tr069CpeDeviceUpdate(product_class="Gateway"),
    )
    assert updated.product_class == "Gateway"


def test_update_cpe_device_rejects_missing_link_target(db_session, acs_server):
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="CPE-LINK-MISSING",
        ),
    )

    with pytest.raises(HTTPException, match="CPE device not found") as exc_info:
        tr069_service.cpe_devices.update(
            db_session,
            device.id,
            Tr069CpeDeviceUpdate(cpe_device_id=uuid4()),
        )

    assert exc_info.value.status_code == 404


def test_update_cpe_device_rejects_parked_inventory_cpe(
    db_session, acs_server, subscriber
):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="TR069 Service OLT", hostname="tr069-service-olt.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/8"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="SERVICE-PARKED-ONT"),
    )
    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            account_id=subscriber.id,
        ),
    )
    network_service.ont_assignments.delete(db_session, str(assignment.id))
    parked_cpe = db_session.scalars(
        select(CPEDevice)
        .where(CPEDevice.serial_number == "SERVICE-PARKED-ONT")
        .limit(1)
    ).first()
    assert parked_cpe is not None

    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="CPE-LINK-PARKED",
        ),
    )

    with pytest.raises(
        HTTPException, match="Cannot link TR-069 device to parked inventory CPE"
    ) as exc_info:
        tr069_service.cpe_devices.update(
            db_session,
            device.id,
            Tr069CpeDeviceUpdate(cpe_device_id=parked_cpe.id),
        )

    assert exc_info.value.status_code == 400


def test_create_tr069_job(db_session, acs_server):
    """Test creating a TR-069 job."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="JOB-CPE",
        ),
    )
    job = tr069_service.jobs.create(
        db_session,
        Tr069JobCreate(
            device_id=device.id,
            name="Get Software Version",
            command="GetParameterValues",
        ),
    )
    assert job.device_id == device.id
    assert job.name == "Get Software Version"


def test_create_ont_from_tr069_device_clears_previous_active_link(db_session):
    old_server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(name="Old ACS", base_url="https://old-acs.example.com"),
    )
    new_server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(name="New ACS", base_url="https://new-acs.example.com"),
    )
    ont = OntUnit(
        serial_number="HWTC12345678",
        tr069_acs_server_id=old_server.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.commit()

    stale_link = Tr069CpeDevice(
        acs_server_id=old_server.id,
        ont_unit_id=ont.id,
        serial_number="HWTC12345678",
        genieacs_device_id="OLD-DEVICE",
        is_active=True,
    )
    candidate = Tr069CpeDevice(
        acs_server_id=new_server.id,
        serial_number="HWTC12345678",
        genieacs_device_id="NEW-DEVICE",
        is_active=True,
    )
    db_session.add_all([stale_link, candidate])
    db_session.commit()

    resolved, created = web_network_tr069_service.create_ont_from_tr069_device(
        db_session,
        tr069_device_id=str(candidate.id),
    )

    db_session.refresh(ont)
    db_session.refresh(stale_link)
    db_session.refresh(candidate)

    assert created is False
    assert resolved.id == ont.id
    assert ont.tr069_acs_server_id == new_server.id
    assert stale_link.ont_unit_id is None
    assert candidate.ont_unit_id == ont.id


def test_link_tr069_device_to_ont_refreshes_previous_ont_snapshot(db_session, acs_server):
    old_ont = OntUnit(
        serial_number="ONT-OLD-SNAPSHOT",
        is_active=True,
        tr069_acs_server_id=acs_server.id,
    )
    new_ont = OntUnit(
        serial_number="ONT-NEW-SNAPSHOT",
        is_active=True,
        tr069_acs_server_id=acs_server.id,
    )
    db_session.add_all([old_ont, new_ont])
    db_session.flush()

    device = Tr069CpeDevice(
        acs_server_id=acs_server.id,
        ont_unit_id=old_ont.id,
        serial_number="TR069-MOVE-001",
        genieacs_device_id="MOVE-DEVICE-001",
        last_inform_at=datetime.now(UTC),
        is_active=True,
    )
    db_session.add(device)
    db_session.commit()

    tr069_service.refresh_ont_status_snapshot(db_session, old_ont)
    db_session.commit()
    db_session.refresh(old_ont)
    assert old_ont.acs_last_inform_at is not None

    tr069_service.link_tr069_device_to_ont(db_session, device, new_ont)
    db_session.commit()
    db_session.refresh(device)
    db_session.refresh(old_ont)
    db_session.refresh(new_ont)

    assert device.ont_unit_id == new_ont.id
    assert old_ont.acs_last_inform_at is None
    assert old_ont.acs_status.value == "unknown"
    assert new_ont.acs_last_inform_at is not None
    assert new_ont.acs_status.value == "online"


def test_update_olt_reassigns_linked_tr069_devices_to_new_acs(db_session):
    old_server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(name="Old ACS", base_url="https://old-olt-acs.example.com"),
    )
    new_server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(name="New ACS", base_url="https://new-olt-acs.example.com"),
    )
    olt = OLTDevice(
        name="OLT-ACS-Move",
        mgmt_ip="198.51.100.90",
        tr069_acs_server_id=old_server.id,
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()

    ont = OntUnit(
        serial_number="ONT-ACS-MOVE-1",
        olt_device_id=olt.id,
        tr069_acs_server_id=old_server.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.commit()

    linked = Tr069CpeDevice(
        acs_server_id=old_server.id,
        ont_unit_id=ont.id,
        serial_number=ont.serial_number,
        genieacs_device_id="ACS-MOVE-DEVICE",
        is_active=True,
    )
    db_session.add(linked)
    db_session.commit()

    values = {
        "name": olt.name,
        "hostname": None,
        "mgmt_ip": olt.mgmt_ip,
        "vendor": None,
        "model": None,
        "serial_number": None,
        "ssh_username": None,
        "ssh_password": None,
        "ssh_port": 22,
        "snmp_enabled": False,
        "snmp_port": 161,
        "snmp_version": "v2c",
        "snmp_community": None,
        "snmp_rw_community": None,
        "netconf_enabled": False,
        "netconf_port": 830,
        "tr069_acs_server_id": str(new_server.id),
        "notes": None,
        "is_active": True,
    }

    with patch.object(
        web_network_olts_service, "_queue_acs_propagation", return_value=None
    ), patch.object(
        web_network_olts_service, "sync_monitoring_device", return_value=None
    ):
        updated, error = web_network_olts_service.update_olt(
            db_session,
            str(olt.id),
            values,
        )

    assert error is None
    assert updated is not None

    db_session.refresh(ont)
    db_session.refresh(linked)
    assert ont.tr069_acs_server_id == new_server.id
    assert linked.acs_server_id == new_server.id


def test_tr069_job_status_transitions(db_session, acs_server):
    """Test TR-069 job status transitions."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="STATUS-CPE",
        ),
    )
    job = tr069_service.jobs.create(
        db_session,
        Tr069JobCreate(
            device_id=device.id,
            name="Reboot",
            command="Reboot",
            status=Tr069JobStatus.queued,
        ),
    )
    assert job.status == Tr069JobStatus.queued

    updated = tr069_service.jobs.update(
        db_session,
        job.id,
        Tr069JobUpdate(status=Tr069JobStatus.succeeded),
    )
    assert updated.status == Tr069JobStatus.succeeded


def test_execute_tr069_job_logs_structured_lifecycle(
    db_session, acs_server, monkeypatch, caplog
):
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="JOB-LOG-CPE",
            oui="HWTC",
            product_class="HG8546M",
        ),
    )
    job = tr069_service.jobs.create(
        db_session,
        Tr069JobCreate(
            device_id=device.id,
            name="Reboot",
            command="reboot",
            status=Tr069JobStatus.queued,
        ),
    )

    class _FakeClient:
        def __init__(self, _base_url):
            return None

        def build_device_id(self, oui, product_class, serial_number):
            return f"{oui}-{product_class}-{serial_number}"

        def create_task(self, _device_id, _task):
            return {"_id": "task-1"}

    monkeypatch.setattr(tr069_service, "GenieACSClient", _FakeClient)
    caplog.set_level("INFO")

    updated = tr069_service.jobs.execute(db_session, str(job.id))

    assert updated.status == Tr069JobStatus.succeeded
    start_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "tr069_job_execute_start"
    )
    complete_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "tr069_job_execute_complete"
    )
    assert start_record.job_id == str(job.id)
    assert start_record.event == "tr069_job"
    assert start_record.serial_number == "JOB-LOG-CPE"
    assert complete_record.job_status == Tr069JobStatus.succeeded.value


def test_create_tr069_parameter(db_session, acs_server):
    """Test creating a TR-069 parameter."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="PARAM-CPE",
        ),
    )
    param = tr069_service.parameters.create(
        db_session,
        Tr069ParameterCreate(
            device_id=device.id,
            name="Device.DeviceInfo.Manufacturer",
            value="TestVendor",
            updated_at=datetime.now(UTC),
        ),
    )
    assert param.device_id == device.id
    assert param.name == "Device.DeviceInfo.Manufacturer"
    assert param.value == "TestVendor"


def test_list_parameters_by_device(db_session, acs_server):
    """Test listing parameters by device."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="LIST-PARAM-CPE",
        ),
    )
    now = datetime.now(UTC)
    tr069_service.parameters.create(
        db_session,
        Tr069ParameterCreate(
            device_id=device.id,
            name="Device.DeviceInfo.SerialNumber",
            value="123",
            updated_at=now,
        ),
    )
    tr069_service.parameters.create(
        db_session,
        Tr069ParameterCreate(
            device_id=device.id,
            name="Device.DeviceInfo.ModelName",
            value="TestModel",
            updated_at=now,
        ),
    )

    params = tr069_service.parameters.list(
        db_session,
        device_id=str(device.id),
        order_by="updated_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(params) >= 2
    assert all(p.device_id == device.id for p in params)


def test_create_tr069_session(db_session, acs_server):
    """Test creating a TR-069 session."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="SESSION-CPE",
        ),
    )
    session = tr069_service.sessions.create(
        db_session,
        Tr069SessionCreate(
            device_id=device.id,
            event_type=Tr069Event.periodic,
            started_at=datetime.now(UTC),
        ),
    )
    assert session.device_id == device.id
    assert session.event_type == Tr069Event.periodic


def test_list_sessions_by_device(db_session, acs_server):
    """Test listing sessions by device."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="LIST-SESS-CPE",
        ),
    )
    now = datetime.now(UTC)
    tr069_service.sessions.create(
        db_session,
        Tr069SessionCreate(
            device_id=device.id,
            event_type=Tr069Event.boot,
            started_at=now,
        ),
    )
    tr069_service.sessions.create(
        db_session,
        Tr069SessionCreate(
            device_id=device.id,
            event_type=Tr069Event.periodic,
            started_at=now,
        ),
    )

    sessions = tr069_service.sessions.list(
        db_session,
        device_id=str(device.id),
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(sessions) >= 2
    assert all(s.device_id == device.id for s in sessions)


def test_delete_cpe_device(db_session, acs_server):
    """Test deleting a CPE device."""
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="DELETE-CPE",
        ),
    )
    tr069_service.cpe_devices.delete(db_session, device.id)
    db_session.refresh(device)
    assert device.is_active is False


def test_get_acs_server(db_session):
    """Test getting an ACS server by ID."""
    server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(
            name="Get Test ACS",
            cwmp_url="https://get.test.local/cwmp",
            base_url="https://get.test.local",
        ),
    )
    fetched = tr069_service.acs_servers.get(db_session, server.id)
    assert fetched is not None
    assert fetched.id == server.id
    assert fetched.name == "Get Test ACS"


def test_delete_acs_server(db_session):
    """Test soft deleting an ACS server."""
    server = tr069_service.acs_servers.create(
        db_session,
        _acs_server_payload(
            name="To Delete ACS",
            cwmp_url="https://delete.acs.local/cwmp",
            base_url="https://delete.acs.local",
        ),
    )
    tr069_service.acs_servers.delete(db_session, server.id)
    db_session.refresh(server)
    assert server.is_active is False


def test_sync_from_genieacs_truncates_long_oui(db_session, acs_server):
    """Sync should not fail when GenieACS _id has long OUI/product_class segments."""
    fake_devices = [
        {
            "_id": "LONGOUIVALUE-LongProductClass-SN12345",
            "_lastInform": "2026-03-03T23:54:59.764Z",
        }
    ]

    with patch("app.services.tr069.GenieACSClient") as mock_client_cls:
        client = mock_client_cls.return_value
        client.list_devices.return_value = fake_devices
        # Keep parser behavior that returns long segments.
        client.parse_device_id.return_value = (
            "LONGOUIVALUE",
            "LongProductClass",
            "SN12345",
        )
        client.extract_parameter_value.return_value = None

        result = tr069_service.cpe_devices.sync_from_genieacs(db_session, str(acs_server.id))

    assert result["created"] == 1
    created = tr069_service.cpe_devices.list(
        db_session,
        acs_server_id=str(acs_server.id),
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )[0]
    assert created.oui == "LONGOUIV"  # truncated to 8 chars
    assert created.product_class == "LongProductClass"


def test_sync_from_genieacs_skips_discoveryservice(db_session, acs_server):
    """Sync should skip DISCOVERYSERVICE phantom devices from GenieACS."""
    fake_devices = [
        {
            "_id": "DISCOVERYSERVICE-DISCOVERYSERVICE-brxvwNZRjQ",
            "_lastInform": "2026-03-03T23:54:59.764Z",
        }
    ]

    with patch("app.services.tr069.GenieACSClient") as mock_client_cls:
        client = mock_client_cls.return_value
        client.list_devices.return_value = fake_devices
        client.parse_device_id.return_value = (
            "DISCOVERYSERVICE",
            "DISCOVERYSERVICE",
            "brxvwNZRjQ",
        )
        client.extract_parameter_value.return_value = None

        result = tr069_service.cpe_devices.sync_from_genieacs(db_session, str(acs_server.id))

    assert result["created"] == 0


def test_create_acs_server_requires_reachable_genieacs(db_session):
    """Create should validate GenieACS endpoint before persisting."""
    values = {
        "name": "GenieACS",
        "base_url": "http://localhost:7557",
        "is_active": True,
        "notes": None,
    }

    with patch("app.services.web_network_tr069.GenieACSClient") as mock_client_cls:
        mock_client_cls.return_value.count_devices.side_effect = GenieACSError("Request error: Connection refused")

        with pytest.raises(ValueError) as exc_info:
            web_network_tr069_service.create_acs_server(db_session, values)

    assert "Failed to connect to GenieACS" in str(exc_info.value)


def test_queue_bulk_action_uses_correlated_enqueue(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_enqueue(task, *, args=None, kwargs=None, correlation_id=None, source=None, **extra):
        captured["task"] = task
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["correlation_id"] = correlation_id
        captured["source"] = source
        captured["extra"] = extra
        return type("AsyncResult", (), {"id": "task-bulk-1"})()

    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    task_id = web_network_tr069_service.queue_bulk_action(
        ["device-1", "device-2"],
        "reboot",
    )

    assert task_id == "task-bulk-1"
    assert captured["args"] == [["device-1", "device-2"], "reboot", {}]
    assert captured["kwargs"] is None
    assert captured["correlation_id"] == "tr069_bulk:reboot:2"
    assert captured["source"] == "web_network_tr069"
