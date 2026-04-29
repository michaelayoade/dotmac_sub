from __future__ import annotations

from app.models.network import OntUnit
from app.services import tr069_auth


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
