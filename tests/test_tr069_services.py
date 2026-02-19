"""Tests for TR-069 service."""

from datetime import UTC, datetime

from app.models.tr069 import Tr069Event, Tr069JobStatus
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
from app.services import tr069 as tr069_service


def test_create_acs_server(db_session):
    """Test creating an ACS server."""
    server = tr069_service.acs_servers.create(
        db_session,
        Tr069AcsServerCreate(
            name="GenieACS",
            base_url="https://acs.example.com",
        ),
    )
    assert server.name == "GenieACS"
    assert server.base_url == "https://acs.example.com"


def test_update_acs_server(db_session):
    """Test updating an ACS server."""
    server = tr069_service.acs_servers.create(
        db_session,
        Tr069AcsServerCreate(name="Original ACS", base_url="https://old.acs.com"),
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
        Tr069AcsServerCreate(name="ACS 1", base_url="https://acs1.com"),
    )
    tr069_service.acs_servers.create(
        db_session,
        Tr069AcsServerCreate(name="ACS 2", base_url="https://acs2.com"),
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
        Tr069AcsServerCreate(
            name="Get Test ACS",
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
        Tr069AcsServerCreate(
            name="To Delete ACS",
            base_url="https://delete.acs.local",
        ),
    )
    tr069_service.acs_servers.delete(db_session, server.id)
    db_session.refresh(server)
    assert server.is_active is False
