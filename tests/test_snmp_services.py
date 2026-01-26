"""Tests for SNMP service."""

from datetime import datetime, timezone

from app.models.snmp import SnmpVersion, SnmpAuthProtocol, SnmpPrivProtocol
from app.schemas.snmp import (
    SnmpCredentialCreate, SnmpCredentialUpdate,
    SnmpTargetCreate, SnmpTargetUpdate,
    SnmpOidCreate, SnmpOidUpdate,
    SnmpPollerCreate, SnmpPollerUpdate,
    SnmpReadingCreate,
)
from app.services import snmp as snmp_service


def test_create_snmp_credential(db_session):
    """Test creating an SNMP credential."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="SNMPv2c Community",
            version=SnmpVersion.v2c,
            community_hash="public",
        ),
    )
    assert credential.name == "SNMPv2c Community"
    assert credential.version == SnmpVersion.v2c
    assert credential.community_hash == "public"


def test_create_snmp_credential_v3(db_session):
    """Test creating an SNMPv3 credential with auth/priv."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="SNMPv3 Auth Priv",
            version=SnmpVersion.v3,
            username="snmpuser",
            auth_protocol=SnmpAuthProtocol.sha,
            auth_secret_hash="authsecret",
            priv_protocol=SnmpPrivProtocol.aes,
            priv_secret_hash="privsecret",
        ),
    )
    assert credential.version == SnmpVersion.v3
    assert credential.auth_protocol == SnmpAuthProtocol.sha
    assert credential.priv_protocol == SnmpPrivProtocol.aes


def test_list_snmp_credentials(db_session):
    """Test listing SNMP credentials."""
    snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Credential 1",
            version=SnmpVersion.v2c,
        ),
    )
    snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Credential 2",
            version=SnmpVersion.v3,
        ),
    )

    credentials = snmp_service.snmp_credentials.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(credentials) >= 2


def test_update_snmp_credential(db_session):
    """Test updating an SNMP credential."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Original Credential",
            version=SnmpVersion.v2c,
        ),
    )
    updated = snmp_service.snmp_credentials.update(
        db_session,
        credential.id,
        SnmpCredentialUpdate(name="Updated Credential"),
    )
    assert updated.name == "Updated Credential"


def test_delete_snmp_credential(db_session):
    """Test deleting an SNMP credential."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="To Delete",
            version=SnmpVersion.v2c,
        ),
    )
    snmp_service.snmp_credentials.delete(db_session, credential.id)
    db_session.refresh(credential)
    assert credential.is_active is False


def test_create_snmp_target(db_session):
    """Test creating an SNMP target."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Target Credential",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="router.example.com",
            mgmt_ip="192.168.1.1",
            port=161,
        ),
    )
    assert target.credential_id == credential.id
    assert target.hostname == "router.example.com"
    assert target.port == 161


def test_list_snmp_targets(db_session):
    """Test listing SNMP targets."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Targets Credential",
            version=SnmpVersion.v2c,
        ),
    )
    snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="target1.local",
        ),
    )
    snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="target2.local",
        ),
    )

    targets = snmp_service.snmp_targets.list(
        db_session,
        device_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(targets) >= 2


def test_update_snmp_target(db_session):
    """Test updating an SNMP target."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Update Target Cred",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="original.host",
        ),
    )
    updated = snmp_service.snmp_targets.update(
        db_session,
        target.id,
        SnmpTargetUpdate(hostname="updated.host", notes="Updated target"),
    )
    assert updated.hostname == "updated.host"
    assert updated.notes == "Updated target"


def test_delete_snmp_target(db_session):
    """Test deleting an SNMP target."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Delete Target Cred",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="delete.target",
        ),
    )
    snmp_service.snmp_targets.delete(db_session, target.id)
    db_session.refresh(target)
    assert target.is_active is False


def test_create_snmp_oid(db_session):
    """Test creating an SNMP OID."""
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(
            name="ifInOctets",
            oid="1.3.6.1.2.1.2.2.1.10",
            unit="bytes",
            description="Input bytes on interface",
        ),
    )
    assert oid.name == "ifInOctets"
    assert oid.oid == "1.3.6.1.2.1.2.2.1.10"
    assert oid.unit == "bytes"


def test_list_snmp_oids(db_session):
    """Test listing SNMP OIDs."""
    snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(
            name="sysUpTime",
            oid="1.3.6.1.2.1.1.3",
        ),
    )
    snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(
            name="sysDescr",
            oid="1.3.6.1.2.1.1.1",
        ),
    )

    oids = snmp_service.snmp_oids.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(oids) >= 2


def test_update_snmp_oid(db_session):
    """Test updating an SNMP OID."""
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(
            name="Original OID",
            oid="1.3.6.1.2.1.1.1",
        ),
    )
    updated = snmp_service.snmp_oids.update(
        db_session,
        oid.id,
        SnmpOidUpdate(name="Updated OID", description="Updated description"),
    )
    assert updated.name == "Updated OID"
    assert updated.description == "Updated description"


def test_delete_snmp_oid(db_session):
    """Test deleting an SNMP OID."""
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(
            name="To Delete OID",
            oid="1.3.6.1.2.1.1.2",
        ),
    )
    snmp_service.snmp_oids.delete(db_session, oid.id)
    db_session.refresh(oid)
    assert oid.is_active is False


def test_create_snmp_poller(db_session):
    """Test creating an SNMP poller."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Poller Credential",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="poller.target",
        ),
    )
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(
            name="ifOutOctets",
            oid="1.3.6.1.2.1.2.2.1.16",
        ),
    )
    poller = snmp_service.snmp_pollers.create(
        db_session,
        SnmpPollerCreate(
            target_id=target.id,
            oid_id=oid.id,
            poll_interval_sec=300,
        ),
    )
    assert poller.target_id == target.id
    assert poller.oid_id == oid.id
    assert poller.poll_interval_sec == 300


def test_list_pollers_by_target(db_session):
    """Test listing SNMP pollers by target."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="List Poller Cred",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="list.poller.target",
        ),
    )
    oid1 = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(name="OID 1", oid="1.3.6.1.2.1.1.1"),
    )
    oid2 = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(name="OID 2", oid="1.3.6.1.2.1.1.2"),
    )

    snmp_service.snmp_pollers.create(
        db_session,
        SnmpPollerCreate(target_id=target.id, oid_id=oid1.id),
    )
    snmp_service.snmp_pollers.create(
        db_session,
        SnmpPollerCreate(target_id=target.id, oid_id=oid2.id),
    )

    pollers = snmp_service.snmp_pollers.list(
        db_session,
        target_id=target.id,
        oid_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(pollers) >= 2
    assert all(p.target_id == target.id for p in pollers)


def test_update_snmp_poller(db_session):
    """Test updating an SNMP poller."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Update Poller Cred",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="update.poller.target",
        ),
    )
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(name="Update Poller OID", oid="1.3.6.1.2.1.1.3"),
    )
    poller = snmp_service.snmp_pollers.create(
        db_session,
        SnmpPollerCreate(
            target_id=target.id,
            oid_id=oid.id,
            poll_interval_sec=60,
        ),
    )
    updated = snmp_service.snmp_pollers.update(
        db_session,
        poller.id,
        SnmpPollerUpdate(poll_interval_sec=120),
    )
    assert updated.poll_interval_sec == 120


def test_delete_snmp_poller(db_session):
    """Test deleting an SNMP poller."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Delete Poller Cred",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="delete.poller.target",
        ),
    )
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(name="Delete Poller OID", oid="1.3.6.1.2.1.1.4"),
    )
    poller = snmp_service.snmp_pollers.create(
        db_session,
        SnmpPollerCreate(target_id=target.id, oid_id=oid.id),
    )
    snmp_service.snmp_pollers.delete(db_session, poller.id)
    db_session.refresh(poller)
    assert poller.is_active is False


def test_create_snmp_reading(db_session):
    """Test creating an SNMP reading."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Reading Credential",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="reading.target",
        ),
    )
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(name="Reading OID", oid="1.3.6.1.2.1.1.5"),
    )
    poller = snmp_service.snmp_pollers.create(
        db_session,
        SnmpPollerCreate(target_id=target.id, oid_id=oid.id),
    )
    reading = snmp_service.snmp_readings.create(
        db_session,
        SnmpReadingCreate(
            poller_id=poller.id,
            value=12345,
            recorded_at=datetime.now(timezone.utc),
        ),
    )
    assert reading.poller_id == poller.id
    assert reading.value == 12345


def test_list_snmp_readings_by_poller(db_session):
    """Test listing SNMP readings by poller."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="List Reading Cred",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="list.reading.target",
        ),
    )
    oid = snmp_service.snmp_oids.create(
        db_session,
        SnmpOidCreate(name="List Reading OID", oid="1.3.6.1.2.1.1.6"),
    )
    poller = snmp_service.snmp_pollers.create(
        db_session,
        SnmpPollerCreate(target_id=target.id, oid_id=oid.id),
    )

    snmp_service.snmp_readings.create(
        db_session,
        SnmpReadingCreate(
            poller_id=poller.id,
            value=100,
            recorded_at=datetime.now(timezone.utc),
        ),
    )
    snmp_service.snmp_readings.create(
        db_session,
        SnmpReadingCreate(
            poller_id=poller.id,
            value=200,
            recorded_at=datetime.now(timezone.utc),
        ),
    )

    readings = snmp_service.snmp_readings.list(
        db_session,
        poller_id=poller.id,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(readings) >= 2
    assert all(r.poller_id == poller.id for r in readings)


def test_get_snmp_credential(db_session):
    """Test getting an SNMP credential by ID."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Get Test Credential",
            version=SnmpVersion.v2c,
            community_hash="public",
        ),
    )
    fetched = snmp_service.snmp_credentials.get(db_session, credential.id)
    assert fetched is not None
    assert fetched.id == credential.id
    assert fetched.name == "Get Test Credential"


def test_get_snmp_target(db_session):
    """Test getting an SNMP target by ID."""
    credential = snmp_service.snmp_credentials.create(
        db_session,
        SnmpCredentialCreate(
            name="Get Target Cred",
            version=SnmpVersion.v2c,
        ),
    )
    target = snmp_service.snmp_targets.create(
        db_session,
        SnmpTargetCreate(
            credential_id=credential.id,
            hostname="get.target.host",
        ),
    )
    fetched = snmp_service.snmp_targets.get(db_session, target.id)
    assert fetched is not None
    assert fetched.id == target.id
    assert fetched.hostname == "get.target.host"
