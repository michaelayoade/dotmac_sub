import hashlib
import uuid

import pytest
from fastapi import HTTPException

from app.models.auth import SessionStatus
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyUpdate,
    MFAMethodCreate,
    MFAMethodUpdate,
    SessionCreate,
    UserCredentialCreate,
    UserCredentialUpdate,
)
from app.services import auth as auth_service
from app.services.auth_flow import hash_password


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, _seconds):
        return True


class _FakeRedisLimit:
    def __init__(self, count):
        self.count = count

    def incr(self, _key):
        return self.count

    def expire(self, _key, _seconds):
        return True


def test_user_credentials_soft_delete(db_session, person):
    payload = UserCredentialCreate(
        person_id=person.id,
        username="user@example.com",
        password_hash=hash_password("secret"),
    )
    credential = auth_service.user_credentials.create(db_session, payload)
    active = auth_service.user_credentials.list(
        db_session,
        person_id=str(person.id),
        provider=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    assert len(active) == 1
    auth_service.user_credentials.delete(db_session, str(credential.id))
    active = auth_service.user_credentials.list(
        db_session,
        person_id=str(person.id),
        provider=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    inactive = auth_service.user_credentials.list(
        db_session,
        person_id=str(person.id),
        provider=None,
        is_active=False,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    assert active == []
    assert len(inactive) == 1


def test_user_credentials_require_valid_person(db_session):
    payload = UserCredentialCreate(
        person_id=uuid.uuid4(),
        username="user@example.com",
        password_hash=hash_password("secret"),
    )
    with pytest.raises(HTTPException) as exc:
        auth_service.user_credentials.create(db_session, payload)
    assert exc.value.status_code == 404


def test_mfa_primary_switch(db_session, person):
    payload = MFAMethodCreate(
        person_id=person.id,
        method_type="totp",
        label="primary",
        secret="encrypted",
        is_primary=True,
        enabled=True,
    )
    first = auth_service.mfa_methods.create(db_session, payload)
    second = auth_service.mfa_methods.create(
        db_session,
        MFAMethodCreate(
            person_id=person.id,
            method_type="totp",
            label="secondary",
            secret="encrypted2",
            is_primary=True,
            enabled=True,
        ),
    )
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.is_primary is False
    assert second.is_primary is True


def test_mfa_update_primary_clears_previous(db_session, person):
    first = auth_service.mfa_methods.create(
        db_session,
        MFAMethodCreate(
            person_id=person.id,
            method_type="totp",
            label="primary",
            secret="encrypted",
            is_primary=True,
            enabled=True,
        ),
    )
    second = auth_service.mfa_methods.create(
        db_session,
        MFAMethodCreate(
            person_id=person.id,
            method_type="totp",
            label="secondary",
            secret="encrypted2",
            is_primary=False,
            enabled=True,
        ),
    )
    updated = auth_service.mfa_methods.update(
        db_session,
        str(second.id),
        MFAMethodUpdate(
            person_id=person.id,
            is_primary=True,
        ),
    )
    db_session.refresh(first)
    db_session.refresh(updated)
    assert first.is_primary is False
    assert updated.is_primary is True


def test_session_delete_revokes(db_session, person):
    payload = SessionCreate(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        ip_address="127.0.0.1",
        user_agent="pytest",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    session = auth_service.sessions.create(db_session, payload)
    auth_service.sessions.delete(db_session, str(session.id))
    db_session.refresh(session)
    assert session.status == SessionStatus.revoked
    assert session.revoked_at is not None


def test_user_credentials_update_requires_radius_server(db_session, person):
    payload = UserCredentialCreate(
        person_id=person.id,
        username="user@example.com",
        password_hash=hash_password("secret"),
    )
    credential = auth_service.user_credentials.create(db_session, payload)
    update = UserCredentialUpdate(radius_server_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        auth_service.user_credentials.update(db_session, str(credential.id), update)
    assert exc.value.status_code == 404


def test_api_key_generate_with_redis(monkeypatch, db_session):
    fake = _FakeRedis()
    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: fake)
    payload = ApiKeyGenerateRequest(label="test")
    result = auth_service.api_keys.generate_with_rate_limit(db_session, payload, None)
    raw_key = result["key"]
    api_key = result["api_key"]
    assert hashlib.sha256(raw_key.encode("utf-8")).hexdigest() == api_key.key_hash


def test_api_key_rate_limit_requires_redis(monkeypatch, db_session):
    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: None)
    with pytest.raises(HTTPException) as exc:
        auth_service.api_keys.generate_with_rate_limit(
            db_session, ApiKeyGenerateRequest(label="test"), None
        )
    assert exc.value.status_code == 503


def test_api_key_rate_limit_exceeded(monkeypatch, db_session):
    fake = _FakeRedisLimit(count=2)
    monkeypatch.setattr(auth_service, "_get_redis_client", lambda: fake)
    monkeypatch.setattr(auth_service, "_auth_int_setting", lambda _db, key, default: 1)
    with pytest.raises(HTTPException) as exc:
        auth_service.api_keys.generate_with_rate_limit(
            db_session, ApiKeyGenerateRequest(label="test"), None
        )
    assert exc.value.status_code == 429


def test_api_key_update_and_revoke(db_session, person):
    created = auth_service.api_keys.create(
        db_session,
        ApiKeyCreate(
            person_id=person.id,
            label="test",
            key_hash="raw-key",
        ),
    )
    updated = auth_service.api_keys.update(
        db_session,
        str(created.id),
        ApiKeyUpdate(key_hash="new-key"),
    )
    assert updated.key_hash == hashlib.sha256(b"new-key").hexdigest()

    auth_service.api_keys.revoke(db_session, str(created.id))
    db_session.refresh(created)
    assert created.is_active is False
    assert created.revoked_at is not None
