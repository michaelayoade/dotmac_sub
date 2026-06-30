import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.auth import MFAMethod, MFAMethodType, SessionStatus
from app.models.auth import Session as AuthSession
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


def test_profile_state_lists_active_system_user_sessions(db_session):
    system_user = SystemUser(
        first_name="Session",
        last_name="Owner",
        email="session-owner@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    current = AuthSession(
        system_user_id=system_user.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"current-token").hexdigest(),
        ip_address="203.0.113.10",
        user_agent="Current Browser",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    other = AuthSession(
        system_user_id=system_user.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"other-token").hexdigest(),
        ip_address="203.0.113.11",
        user_agent="Other Browser",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([current, other])
    db_session.commit()

    state = web_system_profiles_service.build_profile_page_state(
        db_session,
        current_user={"person_id": str(system_user.id)},
        system_user_id=system_user.id,
        current_session_id=str(current.id),
    )

    assert len(state["active_sessions"]) == 2
    assert state["other_session_count"] == 1
    assert any(session.is_current for session in state["active_sessions"])


def test_profile_template_includes_active_sessions_controls():
    template = Path("templates/admin/system/profile.html").read_text()

    assert "Active Sessions" in template
    assert "/admin/system/users/profile/sessions/sign-out-others" in template
    assert "Sign out other sessions" in template
    assert "session.is_current" in template


def test_profile_sign_out_other_sessions_keeps_current(db_session):
    system_user = SystemUser(
        first_name="Session",
        last_name="Revoker",
        email="session-revoker@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    current = AuthSession(
        system_user_id=system_user.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"current-revoke-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    other = AuthSession(
        system_user_id=system_user.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(b"other-revoke-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([current, other])
    db_session.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "principal_type": "system_user",
                "principal_id": str(system_user.id),
                "session_id": str(current.id),
            }
        )
    )

    response = admin_system.user_profile_sign_out_other_sessions(request, db=db_session)

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/admin/system/users/profile?sessions=signed-out"
    )
    db_session.refresh(current)
    db_session.refresh(other)
    assert current.status == SessionStatus.active
    assert other.status == SessionStatus.revoked


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
