import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.auth import (
    ApiKey,
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius import RadiusServer
from app.models.subscription_engine import SettingValueType
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyUpdate,
    UserCredentialCreate,
    UserCredentialUpdate,
)
from app.services import auth as auth_service
from app.services import credential_administration as credential_admin
from app.services.common import apply_ordering, apply_pagination, validate_enum
from tests import credential_administration_fixtures as credential_commands


class _FakeRedisClient:
    def __init__(self):
        self.ping_called = False

    def ping(self):
        self.ping_called = True


def test_auth_setting_and_int_setting(db_session):
    setting_text = DomainSetting(
        domain=SettingDomain.auth,
        key="auth_text",
        value_type=SettingValueType.string,
        value_text="value",
        is_active=True,
    )
    setting_json = DomainSetting(
        domain=SettingDomain.auth,
        key="auth_json",
        value_type=SettingValueType.json,
        value_json={"k": "v"},
        is_active=True,
    )
    setting_bad = DomainSetting(
        domain=SettingDomain.auth,
        key="auth_int",
        value_type=SettingValueType.string,
        value_text="not-int",
        is_active=True,
    )
    db_session.add_all([setting_text, setting_json, setting_bad])
    db_session.commit()

    assert auth_service._auth_setting(db_session, "auth_text") == "value"
    assert auth_service._auth_setting(db_session, "auth_json") == "{'k': 'v'}"
    assert auth_service._auth_int_setting(db_session, "auth_int", 10) == 10
    assert auth_service._auth_setting(db_session, "missing") is None


def test_apply_ordering_and_pagination(db_session, person):
    cred1 = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="a@example.com",
        password_hash="hash",
        is_active=True,
    )
    cred2 = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="b@example.com",
        password_hash="hash",
        is_active=True,
    )
    db_session.add_all([cred1, cred2])
    db_session.commit()

    query = db_session.query(UserCredential)
    ordered = apply_ordering(
        query, "username", "asc", {"username": UserCredential.username}
    ).all()
    assert [item.username for item in ordered] == ["a@example.com", "b@example.com"]

    paged = apply_pagination(query, limit=1, offset=1).all()
    assert len(paged) == 1

    with pytest.raises(HTTPException):
        apply_ordering(query, "bad", "asc", {"username": UserCredential.username})


def test_validate_enum_and_ensure_helpers(db_session, person):
    assert validate_enum(None, AuthProvider, "provider") is None
    with pytest.raises(HTTPException):
        validate_enum("bad", AuthProvider, "provider")

    server = RadiusServer(name="radius", host="127.0.0.1")
    db_session.add(server)
    db_session.commit()

    auth_service._ensure_person(db_session, str(person.id))
    auth_service._ensure_radius_server(db_session, str(server.id))

    with pytest.raises(HTTPException):
        auth_service._ensure_person(db_session, str(uuid.uuid4()))
    with pytest.raises(HTTPException):
        auth_service._ensure_radius_server(db_session, str(uuid.uuid4()))


def test_get_redis_client_cached(monkeypatch):
    """Test _get_redis_client delegates to centralized get_redis()."""
    fake = _FakeRedisClient()

    # Mock the centralized get_redis function
    monkeypatch.setattr(
        "app.services.auth.get_redis",
        lambda: fake,
    )
    client = auth_service._get_redis_client()
    assert client is fake

    # When get_redis returns None, _get_redis_client should return None
    monkeypatch.setattr(
        "app.services.auth.get_redis",
        lambda: None,
    )
    assert auth_service._get_redis_client() is None


def test_get_redis_client_error(monkeypatch):
    """Test _get_redis_client handles errors from get_redis gracefully."""
    # When get_redis returns None (e.g., circuit breaker open), return None
    monkeypatch.setattr(
        "app.services.auth.get_redis",
        lambda: None,
    )
    assert auth_service._get_redis_client() is None


def test_user_credentials_get_list_delete_errors(db_session):
    with pytest.raises(HTTPException):
        auth_service.user_credentials.get(db_session, str(uuid.uuid4()))
    with pytest.raises(credential_admin.CredentialAdministrationError):
        credential_commands.deactivate(db_session, str(uuid.uuid4()))


def test_user_credentials_update_and_list(db_session, person):
    server = RadiusServer(name="radius", host="127.0.0.2")
    db_session.add(server)
    db_session.commit()

    payload = UserCredentialCreate(
        person_id=person.id,
        provider=AuthProvider.radius,
        username="radius-user",
        radius_server_id=server.id,
    )
    credential = credential_commands.create(db_session, payload)

    listed = auth_service.user_credentials.list(
        db_session,
        person_id=str(person.id),
        provider="radius",
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert credential.id in [row.id for row in listed]

    updated = credential_commands.update(
        db_session,
        str(credential.id),
        UserCredentialUpdate(username="new-radius"),
    )
    assert updated.username == "new-radius"

    fetched = auth_service.user_credentials.get(db_session, str(credential.id))
    assert fetched.id == credential.id


def test_user_credentials_update_not_found(db_session):
    with pytest.raises(credential_admin.CredentialAdministrationError):
        credential_commands.update(
            db_session, str(uuid.uuid4()), UserCredentialUpdate(username="missing")
        )


def test_mfa_get_delete_errors(db_session):
    with pytest.raises(HTTPException):
        auth_service.mfa_methods.get(db_session, str(uuid.uuid4()))
    with pytest.raises(HTTPException):
        auth_service.mfa_methods.delete(
            db_session, str(uuid.uuid4()), actor_id="admin-test"
        )


def test_mfa_list_delete(db_session, person):
    method = MFAMethod(
        subscriber_id=person.id,
        method_type=MFAMethodType.totp,
        label="primary",
        secret="unit-fixture-secret",
        is_primary=True,
        enabled=True,
    )
    db_session.add(method)
    db_session.commit()
    listed = auth_service.mfa_methods.list(
        db_session,
        person_id=str(person.id),
        method_type="totp",
        is_primary=True,
        enabled=True,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert method in listed

    auth_service.mfa_methods.delete(db_session, str(method.id), actor_id="admin-test")
    db_session.refresh(method)
    assert method.is_active is False

    fetched = auth_service.mfa_methods.get(db_session, str(method.id))
    assert fetched.id == method.id


def test_sessions_get_delete_errors(db_session):
    with pytest.raises(HTTPException):
        auth_service.sessions.get(db_session, str(uuid.uuid4()))
    with pytest.raises(HTTPException):
        auth_service.sessions.delete(db_session, str(uuid.uuid4()))


def test_api_keys_get_delete_errors(db_session):
    with pytest.raises(HTTPException):
        auth_service.api_keys.get(db_session, str(uuid.uuid4()))
    with pytest.raises(HTTPException):
        auth_service.api_keys.delete(db_session, str(uuid.uuid4()))


def test_api_key_list_filters(db_session, person):
    active = ApiKey(
        person_id=person.id,
        label="active",
        key_hash=auth_service.hash_api_key("active"),
        is_active=True,
    )
    inactive = ApiKey(
        person_id=person.id,
        label="inactive",
        key_hash=auth_service.hash_api_key("inactive"),
        is_active=False,
    )
    db_session.add_all([active, inactive])
    db_session.commit()

    active_only = auth_service.api_keys.list(
        db_session,
        person_id=str(person.id),
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(active_only) == 1

    all_keys = auth_service.api_keys.list(
        db_session,
        person_id=str(person.id),
        is_active=False,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(all_keys) == 1


def test_sessions_list_get(db_session, person):
    session = AuthSession(
        subscriber_id=person.id,
        token_hash="unit-fixture-hash",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db_session.add(session)
    db_session.commit()
    listed = auth_service.sessions.list(
        db_session,
        person_id=str(person.id),
        status="active",
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert session in listed

    fetched = auth_service.sessions.get(db_session, str(session.id))
    assert fetched.id == session.id


def test_api_key_generate_with_person(db_session, person):
    payload = ApiKeyGenerateRequest(person_id=person.id, label="test")
    api_key, raw = auth_service.api_keys.generate(db_session, payload)
    assert api_key.person_id == person.id
    assert auth_service.hash_api_key(raw) == api_key.key_hash


def test_api_key_generate_rate_limit_redis_error(monkeypatch, db_session):
    class _BoomRedis:
        def incr(self, _key):
            raise auth_service.redis.RedisError("boom")

    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: _BoomRedis())
    with pytest.raises(HTTPException):
        auth_service.api_keys.generate_with_rate_limit(
            db_session, ApiKeyGenerateRequest(label="test"), None
        )


def test_api_key_generate_rate_limit_with_request(monkeypatch, db_session):
    fake = type(
        "Redis",
        (),
        {"incr": lambda self, _key: 1, "expire": lambda self, _key, _window: None},
    )()
    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: fake)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    result = auth_service.api_keys.generate_with_rate_limit(
        db_session, ApiKeyGenerateRequest(label="test"), request
    )
    assert "key" in result


def test_api_keys_get_and_update_person(db_session, person):
    api_key = auth_service.api_keys.create(
        db_session,
        ApiKeyCreate(
            person_id=person.id,
            label="test",
            key_hash="raw-key",
        ),
    )
    fetched = auth_service.api_keys.get(db_session, str(api_key.id))
    assert fetched.id == api_key.id

    updated = auth_service.api_keys.update(
        db_session,
        str(api_key.id),
        ApiKeyUpdate(person_id=person.id),
    )
    assert updated.person_id == person.id


def test_user_credentials_default_provider(monkeypatch, db_session, person):
    monkeypatch.setattr(
        credential_admin, "resolve_value", lambda *args, **kwargs: "radius"
    )
    payload = UserCredentialCreate(
        person_id=person.id,
        username="radius@example.com",
    )
    credential = credential_commands.create(db_session, payload)
    assert credential.provider == AuthProvider.radius
