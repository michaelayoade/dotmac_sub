"""API keys carry scopes; audit auth enforces them (fail-closed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.auth import ApiKey
from app.services.auth import hash_api_key
from app.services.auth_dependencies import require_audit_auth
from app.services.web_system_api_key_forms import create_api_key, parse_scopes


def _make_key(db, *, scopes, raw="raw-scope-key"):
    key = ApiKey(
        label="t",
        key_hash=hash_api_key(raw),
        scopes=scopes,
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db.add(key)
    db.commit()
    return key, raw


def test_parse_scopes_dedup_and_split():
    assert parse_scopes("audit:read, billing:read audit:read") == [
        "audit:read",
        "billing:read",
    ]
    assert parse_scopes(None) == []
    assert parse_scopes("  ") == []


def test_audit_auth_accepts_key_with_audit_scope(db_session):
    key, raw = _make_key(db_session, scopes=["audit:read"])
    auth = require_audit_auth(
        authorization=None, x_session_token=None, x_api_key=raw, db=db_session
    )
    assert auth["actor_type"] == "api_key"
    assert auth["actor_id"] == str(key.id)
    db_session.refresh(key)
    assert key.last_used_at is not None  # use is now stamped


def test_audit_auth_accepts_wildcard_audit_scope(db_session):
    _make_key(db_session, scopes=["audit:*"], raw="wild")
    auth = require_audit_auth(
        authorization=None, x_session_token=None, x_api_key="wild", db=db_session
    )
    assert auth["actor_type"] == "api_key"


def test_audit_auth_rejects_key_without_audit_scope(db_session):
    _make_key(db_session, scopes=["billing:invoice:read"], raw="other")
    with pytest.raises(HTTPException) as exc:
        require_audit_auth(
            authorization=None, x_session_token=None, x_api_key="other", db=db_session
        )
    assert exc.value.status_code == 401


def test_audit_auth_rejects_key_with_no_scopes(db_session):
    _make_key(db_session, scopes=[], raw="empty")
    with pytest.raises(HTTPException) as exc:
        require_audit_auth(
            authorization=None, x_session_token=None, x_api_key="empty", db=db_session
        )
    assert exc.value.status_code == 401


def test_web_create_persists_scopes(db_session):
    create_api_key(
        db_session,
        subscriber_id=None,
        label="scoped",
        expires_in=None,
        scopes=parse_scopes("audit:read,reports:billing"),
    )
    key = db_session.query(ApiKey).filter(ApiKey.label == "scoped").one()
    assert key.scopes == ["audit:read", "reports:billing"]
