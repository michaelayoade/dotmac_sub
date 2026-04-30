from __future__ import annotations

from app.models.catalog import RegionZone
from app.models.network import OLTDevice, OntUnit, Vlan, VlanPurpose
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import tr069_auth
from app.services.credential_crypto import encrypt_credential


def test_tr069_auth_reads_credentials_through_desired_config_helper(db_session) -> None:
    ont = OntUnit(
        serial_number="AUTH-DESIRED-001",
        is_active=True,
        desired_config={
            "connection_request_username": "cr-user",
            "connection_request_password": "plain:cr-pass",
            "cwmp_username": "cwmp-user",
            "cwmp_password": "plain:cwmp-pass",
            "tr069": {"olt_profile_id": 999},
        },
    )
    db_session.add(ont)
    db_session.commit()

    cr = tr069_auth.get_device_credentials(
        db_session,
        "AUTH-DESIRED-001",
        "connection_request",
    )
    cpe = tr069_auth.get_device_credentials(
        db_session,
        "AUTH-DESIRED-001",
        "cpe_auth",
    )

    assert cr == {"username": "cr-user", "password": "cr-pass"}
    assert cpe == {"username": "cwmp-user", "password": "cwmp-pass"}


def test_tr069_auth_reads_effective_olt_config_pack_credentials(db_session) -> None:
    region = RegionZone(name="Auth Effective Region", code="auth-effective")
    acs = Tr069AcsServer(
        name="Effective ACS",
        base_url="http://genieacs.example:7557",
        is_active=True,
        cwmp_username="cwmp-effective",
        cwmp_password=encrypt_credential("cwmp-pass"),
    )
    olt = OLTDevice(name="Auth Effective OLT")
    ont = OntUnit(
        serial_number="AUTH-EFFECTIVE-001",
        olt_device=olt,
        is_active=True,
    )
    db_session.add_all([region, acs, olt, ont])
    db_session.flush()
    olt.tr069_acs_server_id = acs.id
    vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        tag=201,
        name="Management",
        purpose=VlanPurpose.management,
    )
    db_session.add(vlan)
    db_session.flush()
    olt.config_pack = {
        "management_vlan_id": str(vlan.id),
        "tr069_olt_profile_id": 2,
        "cr_username": "cr-effective",
        "cr_password": encrypt_credential("cr-pass"),
    }
    db_session.commit()

    cr = tr069_auth.get_device_credentials(
        db_session,
        "AUTH-EFFECTIVE-001",
        "connection_request",
    )
    cpe = tr069_auth.get_device_credentials(
        db_session,
        "AUTH-EFFECTIVE-001",
        "cpe_auth",
    )

    assert cr == {"username": "cr-effective", "password": "cr-pass"}
    assert cpe == {"username": "cwmp-effective", "password": "cwmp-pass"}


def test_tr069_auth_ignores_inactive_unlinked_cpe_rows(db_session) -> None:
    region = RegionZone(name="Auth Stale Region", code="auth-stale")
    acs = Tr069AcsServer(
        name="Stale ACS",
        base_url="http://genieacs.example:7557",
        is_active=True,
    )
    olt = OLTDevice(name="Auth Stale OLT")
    ont = OntUnit(
        serial_number="HWTCSTALE001",
        olt_device=olt,
        is_active=True,
    )
    db_session.add_all([region, acs, olt, ont])
    db_session.flush()
    stale_cpe = Tr069CpeDevice(
        serial_number="HWTCSTALE001",
        acs_server_id=acs.id,
        is_active=False,
        ont_unit_id=None,
    )
    db_session.add(stale_cpe)
    olt.tr069_acs_server_id = acs.id
    vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        tag=201,
        name="Management",
        purpose=VlanPurpose.management,
    )
    db_session.add(vlan)
    db_session.flush()
    olt.config_pack = {
        "management_vlan_id": str(vlan.id),
        "tr069_olt_profile_id": 2,
        "cr_username": "cr-effective",
        "cr_password": encrypt_credential("cr-pass"),
    }
    db_session.commit()

    cr = tr069_auth.get_device_credentials(
        db_session,
        "HWTCSTALE001",
        "connection_request",
    )

    assert cr == {"username": "cr-effective", "password": "cr-pass"}
