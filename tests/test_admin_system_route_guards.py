from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.auth import MFAMethod, MFAMethodType
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import web_system_profiles as web_system_profiles_service
from app.web.admin import system as admin_system


def test_require_system_user_principal_accepts_system_user():
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_type": "system_user"})
    )

    auth = admin_system._require_system_user_principal(request)

    assert auth["principal_type"] == "system_user"


def test_require_system_user_principal_rejects_subscriber():
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_type": "subscriber"})
    )

    with pytest.raises(HTTPException) as exc:
        admin_system._require_system_user_principal(request)

    assert exc.value.status_code == 403


def test_dbi_principal_id_prefers_stable_actor_id(monkeypatch):
    request = SimpleNamespace()
    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {"subscriber_id": "system-user-1", "id": "system-user-1"},
    )

    assert admin_system._dbi_principal_id(request) == "system-user-1"


def test_profile_state_prefers_system_user_id_for_mfa_status(db_session):
    current_user_record = SystemUser(
        first_name="Current",
        last_name="User",
        email="current@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    stale_user_record = SystemUser(
        first_name="Stale",
        last_name="User",
        email="stale@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add_all([current_user_record, stale_user_record])
    db_session.flush()
    db_session.add(
        MFAMethod(
            system_user_id=current_user_record.id,
            method_type=MFAMethodType.totp,
            label="Authenticator app",
            enabled=True,
            is_primary=True,
            is_active=True,
        )
    )
    db_session.commit()

    state = web_system_profiles_service.build_profile_page_state(
        db_session,
        current_user={"person_id": str(stale_user_record.id)},
        system_user_id=current_user_record.id,
    )

    assert state["person"].id == current_user_record.id
    assert state["mfa_enabled"] is True


def test_profile_mfa_confirm_redirects_with_success_flag(monkeypatch, db_session):
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={"principal_type": "system_user", "principal_id": "system-user-1"}
        )
    )
    captured = {}

    def fake_admin_mfa_confirm(db, method_id, code, system_user_id):
        captured["db"] = db
        captured["method_id"] = method_id
        captured["code"] = code
        captured["system_user_id"] = system_user_id

    monkeypatch.setattr(
        "app.services.auth_flow.auth_flow.admin_mfa_confirm",
        fake_admin_mfa_confirm,
    )

    response = admin_system.user_profile_mfa_confirm(
        request,
        method_id="method-1",
        code=" 123456 ",
        db=db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/system/users/profile?mfa=enabled"
    assert captured == {
        "db": db_session,
        "method_id": "method-1",
        "code": "123456",
        "system_user_id": "system-user-1",
    }
