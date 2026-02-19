import uuid
from datetime import UTC, datetime

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request

from app.models.auth import AuthProvider, MFAMethod, MFAMethodType, UserCredential
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.auth_flow import LogoutResponse, TokenResponse
from app.services import auth_flow as auth_flow_service


def _make_request(cookies: str | None = None):
    headers = [(b"user-agent", b"pytest")]
    if cookies:
        headers.append((b"cookie", cookies.encode("utf-8")))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_env_helpers(monkeypatch):
    monkeypatch.setenv("TEST_VALUE", "123")
    assert auth_flow_service._env_value("TEST_VALUE") == "123"
    assert auth_flow_service._env_int("TEST_VALUE") == 123
    monkeypatch.setenv("TEST_VALUE", "nope")
    assert auth_flow_service._env_int("TEST_VALUE") is None
    monkeypatch.setenv("TEST_VALUE", "")
    assert auth_flow_service._env_value("TEST_VALUE") is None
    assert auth_flow_service._env_int("TEST_VALUE") is None


def test_setting_value_and_jwt_secret_errors(db_session, monkeypatch):
    assert auth_flow_service._setting_value(None, "missing") is None

    setting = DomainSetting(
        domain=SettingDomain.auth,
        key="jwt_algorithm",
        value_type=SettingValueType.string,
        value_text="HS512",
        is_active=True,
    )
    setting_json = DomainSetting(
        domain=SettingDomain.auth,
        key="jwt_refresh_ttl_days",
        value_type=SettingValueType.json,
        value_json={"value": "bad"},
        is_active=True,
    )
    db_session.add(setting)
    db_session.add(setting_json)
    db_session.commit()
    assert auth_flow_service._setting_value(db_session, "jwt_algorithm") == "HS512"
    assert auth_flow_service._setting_value(db_session, "jwt_refresh_ttl_days") == "{'value': 'bad'}"

    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(HTTPException):
        auth_flow_service._jwt_secret(None)


def test_refresh_cookie_settings_env(monkeypatch):
    monkeypatch.setenv("REFRESH_COOKIE_NAME", "refresh")
    monkeypatch.setenv("REFRESH_COOKIE_SECURE", "true")
    monkeypatch.setenv("REFRESH_COOKIE_SAMESITE", "strict")
    monkeypatch.setenv("REFRESH_COOKIE_DOMAIN", "example.com")
    monkeypatch.setenv("REFRESH_COOKIE_PATH", "/auth")
    monkeypatch.setenv("JWT_REFRESH_TTL_DAYS", "2")
    settings = auth_flow_service.AuthFlow.refresh_cookie_settings(None)
    assert settings["key"] == "refresh"
    assert settings["secure"] is True
    assert settings["samesite"] == "strict"
    assert settings["domain"] == "example.com"
    assert settings["path"] == "/auth"
    assert settings["max_age"] == 2 * 24 * 60 * 60


def test_refresh_cookie_settings_from_db(db_session, monkeypatch):
    monkeypatch.delenv("REFRESH_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("JWT_REFRESH_TTL_DAYS", raising=False)
    setting_secure = DomainSetting(
        domain=SettingDomain.auth,
        key="refresh_cookie_secure",
        value_type=SettingValueType.boolean,
        value_text="true",
        is_active=True,
    )
    db_session.add(setting_secure)
    db_session.commit()
    assert auth_flow_service._refresh_cookie_secure(db_session) is True


def test_setting_value_empty_text(db_session):
    setting_empty = DomainSetting(
        domain=SettingDomain.auth,
        key="empty_setting",
        value_type=SettingValueType.string,
        value_text="",
        is_active=True,
    )
    db_session.add(setting_empty)
    db_session.commit()
    assert auth_flow_service._setting_value(db_session, "empty_setting") is None


def test_as_utc_and_ttl_defaults(monkeypatch):
    now = datetime.now(UTC)
    assert auth_flow_service._as_utc(now) == now
    monkeypatch.delenv("JWT_ACCESS_TTL_MINUTES", raising=False)
    monkeypatch.delenv("JWT_REFRESH_TTL_DAYS", raising=False)
    assert auth_flow_service._access_ttl_minutes(None) == 15
    assert auth_flow_service._refresh_ttl_days(None) == 30


def test_refresh_cookie_secure_default_false(monkeypatch):
    monkeypatch.delenv("REFRESH_COOKIE_SECURE", raising=False)
    assert auth_flow_service._refresh_cookie_secure(None) is False


def test_truncate_user_agent():
    short = auth_flow_service._truncate_user_agent("short")
    assert short == "short"
    assert auth_flow_service._truncate_user_agent(None) is None
    long_value = "x" * 600
    assert len(auth_flow_service._truncate_user_agent(long_value)) == 512


def test_mfa_key_and_fernet_errors(monkeypatch):
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", "bad-key")
    with pytest.raises(HTTPException):
        auth_flow_service._fernet(None)


def test_mfa_key_missing(monkeypatch):
    monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)
    with pytest.raises(HTTPException):
        auth_flow_service._mfa_key(None)


def test_encrypt_decrypt_secret(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    secret = "secret"
    encrypted = auth_flow_service._encrypt_secret(None, secret)
    assert auth_flow_service._decrypt_secret(None, encrypted) == secret
    with pytest.raises(HTTPException):
        auth_flow_service._decrypt_secret(None, "invalid")


def test_issue_and_decode_tokens(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    access = auth_flow_service._issue_access_token(
        None, "person", "session", roles=["admin"], permissions=["audit:read"]
    )
    payload = jwt.decode(access, "test-secret", algorithms=["HS256"])
    assert payload["roles"] == ["admin"]
    assert payload["scopes"] == ["audit:read"]

    mfa = auth_flow_service._issue_mfa_token(None, "person")
    with pytest.raises(HTTPException):
        auth_flow_service._decode_jwt(None, mfa, "access")

    with pytest.raises(HTTPException):
        auth_flow_service._decode_jwt(None, "bad-token", "access")


def test_access_and_refresh_ttl_defaults(db_session, monkeypatch):
    monkeypatch.delenv("JWT_ACCESS_TTL_MINUTES", raising=False)
    monkeypatch.delenv("JWT_REFRESH_TTL_DAYS", raising=False)
    bad_access = DomainSetting(
        domain=SettingDomain.auth,
        key="jwt_access_ttl_minutes",
        value_type=SettingValueType.string,
        value_text="bad",
        is_active=True,
    )
    bad_refresh = DomainSetting(
        domain=SettingDomain.auth,
        key="jwt_refresh_ttl_days",
        value_type=SettingValueType.string,
        value_text="bad",
        is_active=True,
    )
    db_session.add_all([bad_access, bad_refresh])
    db_session.commit()
    assert auth_flow_service._access_ttl_minutes(db_session) == 15
    assert auth_flow_service._refresh_ttl_days(db_session) == 30


def test_password_reset_ttl_invalid(db_session):
    setting = DomainSetting(
        domain=SettingDomain.auth,
        key="password_reset_ttl_minutes",
        value_type=SettingValueType.string,
        value_text="bad",
        is_active=True,
    )
    db_session.add(setting)
    db_session.commit()
    assert auth_flow_service._password_reset_ttl_minutes(db_session) == 60


def test_password_reset_ttl_from_env(monkeypatch):
    monkeypatch.setenv("PASSWORD_RESET_TTL_MINUTES", "15")
    assert auth_flow_service._password_reset_ttl_minutes(None) == 15


def test_person_or_404_missing(db_session):
    with pytest.raises(HTTPException):
        auth_flow_service._person_or_404(db_session, str(uuid.uuid4()))


def test_load_rbac_claims_none():
    roles, permissions = auth_flow_service._load_rbac_claims(None, "person")
    assert roles == []
    assert permissions == []


def test_verify_password_none():
    assert auth_flow_service.verify_password("secret", None) is False


def test_refresh_and_logout_responses_missing_token(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    request = _make_request()
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.refresh_response(None, None, request)
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.logout_response(None, None, request)


def test_resolve_refresh_token_from_cookie():
    request = _make_request(cookies="refresh_token=cookie-value")
    resolved = auth_flow_service.AuthFlow.resolve_refresh_token(request, None, None)
    assert resolved == "cookie-value"


def test_response_cookie_helpers(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    payload = {"access_token": "access", "refresh_token": "refresh"}
    response = auth_flow_service.AuthFlow._response_with_refresh_cookie(
        None, payload, TokenResponse
    )
    assert "refresh_token=" in response.headers.get("set-cookie", "")

    cleared = auth_flow_service.AuthFlow._response_clear_refresh_cookie(
        None, {"revoked_at": datetime.now(UTC)}, LogoutResponse
    )
    assert "refresh_token=" in cleared.headers.get("set-cookie", "")


def test_mfa_confirm_and_verify_errors(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    method = MFAMethod(
        person_id=person.id,
        method_type=MFAMethodType.sms,
        label="sms",
        secret="secret",
        is_primary=True,
        enabled=True,
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()

    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_confirm(
            db_session, str(method.id), "123456", str(person.id)
        )

    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_verify(
            db_session, auth_flow_service._issue_access_token(None, "p", "s"), "123456", _make_request()
        )


def test_mfa_confirm_invalid_code(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    secret = pyotp.random_base32()
    method = MFAMethod(
        person_id=person.id,
        method_type=MFAMethodType.totp,
        label="totp",
        secret=auth_flow_service._encrypt_secret(None, secret),
        is_primary=False,
        enabled=False,
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_confirm(
            db_session, str(method.id), "000000", str(person.id)
        )


def test_mfa_confirm_person_mismatch(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    method = MFAMethod(
        person_id=person.id,
        method_type=MFAMethodType.totp,
        label="totp",
        secret=auth_flow_service._encrypt_secret(None, "secret"),
        is_primary=False,
        enabled=False,
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_confirm(
            db_session, str(method.id), "000000", "other-person"
        )


def test_mfa_confirm_not_found(db_session):
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_confirm(
            db_session, str(uuid.uuid4()), "000000", str(uuid.uuid4())
        )


def test_mfa_confirm_commit_error(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    setup = auth_flow_service.AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()

    def _boom():
        raise auth_flow_service.IntegrityError("stmt", "params", "orig")

    monkeypatch.setattr(db_session, "commit", _boom)
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_confirm(
            db_session, str(setup["method_id"]), code, str(person.id)
        )


def test_mfa_verify_missing_method(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    mfa_token = auth_flow_service._issue_mfa_token(None, str(person.id))
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_verify(db_session, mfa_token, "123456", _make_request())


def test_mfa_verify_missing_sub(monkeypatch, db_session):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = jwt.encode(
        {"typ": "mfa", "iat": 1, "exp": 9999999999},
        "test-secret",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_verify(db_session, token, "123456", _make_request())


def test_mfa_verify_invalid_code(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    setup = auth_flow_service.AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()
    auth_flow_service.AuthFlow.mfa_confirm(db_session, str(setup["method_id"]), code, str(person.id))
    mfa_token = auth_flow_service._issue_mfa_token(None, str(person.id))
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.mfa_verify(db_session, mfa_token, "000000", _make_request())


def test_mfa_verify_response_success(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa@example.com",
        password_hash=auth_flow_service.hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    setup = auth_flow_service.AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()
    auth_flow_service.AuthFlow.mfa_confirm(db_session, str(setup["method_id"]), code, str(person.id))
    mfa_token = auth_flow_service._issue_mfa_token(None, str(person.id))
    response = auth_flow_service.AuthFlow.mfa_verify_response(
        db_session, mfa_token, pyotp.TOTP(setup["secret"]).now(), _make_request()
    )
    assert response.status_code == 200


def test_refresh_logout_invalid_token(db_session):
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.refresh(db_session, "invalid", _make_request())
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.logout(db_session, "invalid")


def test_refresh_and_logout_responses_success(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="user@example.com",
        password_hash=auth_flow_service.hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    tokens_refresh = auth_flow_service.AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    response = auth_flow_service.AuthFlow.refresh_response(
        db_session, tokens_refresh["refresh_token"], _make_request()
    )
    assert response.status_code == 200

    tokens_logout = auth_flow_service.AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    logout = auth_flow_service.AuthFlow.logout_response(
        db_session, tokens_logout["refresh_token"], _make_request()
    )
    assert logout.status_code == 200


def test_login_response_mfa_required(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa-login@example.com",
        password_hash=auth_flow_service.hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    setup = auth_flow_service.AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()
    auth_flow_service.AuthFlow.mfa_confirm(db_session, str(setup["method_id"]), code, str(person.id))

    result = auth_flow_service.AuthFlow.login_response(
        db_session, "mfa-login@example.com", "secret", _make_request(), AuthProvider.local
    )
    assert result["mfa_required"] is True


def test_login_response_sets_cookie(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="cookie@example.com",
        password_hash=auth_flow_service.hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    response = auth_flow_service.AuthFlow.login_response(
        db_session, "cookie@example.com", "secret", _make_request(), None
    )
    assert response.status_code == 200
    assert "refresh_token=" in response.headers.get("set-cookie", "")


def test_login_radius_and_invalid_provider(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    called = {"value": False}

    def _fake_authenticate(db, username, password, server_id):
        called["value"] = True

    monkeypatch.setattr(auth_flow_service.radius_auth_service, "authenticate", _fake_authenticate)
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.radius,
        username="radius@example.com",
        password_hash=None,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    auth_flow_service.AuthFlow.login(db_session, "radius@example.com", "secret", _make_request(), AuthProvider.radius)
    assert called["value"] is True

    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.login(db_session, "radius@example.com", "secret", _make_request(), "bad")

    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.login(db_session, "missing@example.com", "secret", _make_request(), AuthProvider.radius)


def test_login_invalid_credentials(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    with pytest.raises(HTTPException):
        auth_flow_service.AuthFlow.login(db_session, "missing@example.com", "secret", _make_request(), None)


def test_password_reset_no_credentials(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    assert auth_flow_service.request_password_reset(db_session, person.email) is None

    token = auth_flow_service._issue_password_reset_token(db_session, str(person.id), person.email)
    with pytest.raises(HTTPException):
        auth_flow_service.reset_password(db_session, token, "new")


def test_password_reset_invalid_person(monkeypatch, db_session):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = auth_flow_service._issue_password_reset_token(
        db_session, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "missing@example.com"
    )
    with pytest.raises(HTTPException):
        auth_flow_service.reset_password(db_session, token, "new")


def test_password_reset_missing_claims(monkeypatch, db_session):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = jwt.encode(
        {"typ": "password_reset", "iat": 1, "exp": 9999999999},
        "test-secret",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        auth_flow_service.reset_password(db_session, token, "new")
