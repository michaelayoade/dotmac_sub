from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException

from app.api import tr069_auth as tr069_auth_api
from app.models.catalog import RegionZone
from app.models.network import (
    OLTDevice,
    OntAuthorizationStatus,
    OntUnit,
    Vlan,
    VlanPurpose,
)
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services import tr069_auth
from app.services.credential_crypto import encrypt_credential


def test_tr069_auth_reads_credentials_through_desired_config_helper(db_session) -> None:
    ont = OntUnit(
        serial_number="AUTH-DESIRED-001",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
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

    assert cr == {"username": "cr-user", "password": "cr-pass", "authorized": True}
    assert cpe == {
        "username": "cwmp-user",
        "password": "cwmp-pass",
        "authorized": True,
    }


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
        authorization_status=OntAuthorizationStatus.authorized,
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

    assert cr == {
        "username": "cr-effective",
        "password": "cr-pass",
        "authorized": True,
    }
    assert cpe == {
        "username": "cwmp-effective",
        "password": "cwmp-pass",
        "authorized": True,
    }


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
        authorization_status=OntAuthorizationStatus.authorized,
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

    assert cr == {
        "username": "cr-effective",
        "password": "cr-pass",
        "authorized": True,
    }


def test_tr069_auth_marks_unknown_serial_unauthorized(db_session) -> None:
    result = tr069_auth.get_device_credentials(
        db_session,
        "UNKNOWN-AUTH-001",
        "cpe_auth",
    )

    assert result == {"username": None, "password": None, "authorized": False}


def test_tr069_auth_marks_deauthorized_ont_unauthorized(db_session) -> None:
    ont = OntUnit(
        serial_number="AUTH-DEAUTH-001",
        is_active=True,
        authorization_status=OntAuthorizationStatus.deauthorized,
        desired_config={
            "cwmp_username": "cwmp-user",
            "cwmp_password": "plain:cwmp-pass",
        },
    )
    db_session.add(ont)
    db_session.commit()

    result = tr069_auth.get_device_credentials(
        db_session,
        "AUTH-DEAUTH-001",
        "cpe_auth",
    )

    assert result == {"username": None, "password": None, "authorized": False}


def test_tr069_auth_api_requires_shared_secret(db_session, monkeypatch) -> None:
    monkeypatch.setattr(
        tr069_auth_api,
        "settings",
        SimpleNamespace(tr069_auth_shared_secret="secret"),
    )

    try:
        tr069_auth_api.get_device_credentials(
            serial_number="AUTH-DESIRED-001",
            type="cpe_auth",
            shared_secret=None,
            db=db_session,
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected HTTPException")


def test_tr069_auth_api_fails_closed_when_secret_not_configured(
    db_session, monkeypatch
) -> None:
    monkeypatch.setattr(
        tr069_auth_api,
        "settings",
        SimpleNamespace(tr069_auth_shared_secret=""),
    )

    try:
        tr069_auth_api.get_device_credentials(
            serial_number="AUTH-DESIRED-001",
            type="cpe_auth",
            shared_secret=None,
            db=db_session,
        )
    except HTTPException as exc:
        assert exc.status_code == 503
    else:
        raise AssertionError("expected HTTPException")


def test_tr069_auth_api_accepts_valid_shared_secret(db_session, monkeypatch) -> None:
    monkeypatch.setattr(
        tr069_auth_api,
        "settings",
        SimpleNamespace(tr069_auth_shared_secret="secret"),
    )

    result = tr069_auth_api.get_device_credentials(
        serial_number="UNKNOWN-AUTH-001",
        type="cpe_auth",
        shared_secret="secret",
        db=db_session,
    )

    assert result == {"username": None, "password": None, "authorized": False}
