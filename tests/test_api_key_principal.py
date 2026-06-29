"""API keys are first-class principals on require_permission endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.auth import ApiKey
from app.services.auth import hash_api_key
from app.services.auth_dependencies import require_permission, require_user_auth


def _make_key(db, *, scopes, raw):
    db.add(
        ApiKey(
            label="t",
            key_hash=hash_api_key(raw),
            scopes=scopes,
            is_active=True,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db.commit()


def test_require_user_auth_accepts_api_key(db_session):
    _make_key(db_session, scopes=["audit:read"], raw="pk1")
    auth = require_user_auth(authorization=None, x_api_key="pk1", db=db_session)
    assert auth["principal_type"] == "api_key"
    assert auth["roles"] == []
    assert auth["scopes"] == ["audit:read"]


def test_require_user_auth_rejects_unknown_key(db_session):
    with pytest.raises(HTTPException) as exc:
        require_user_auth(authorization=None, x_api_key="nope", db=db_session)
    assert exc.value.status_code == 401


def test_require_permission_honors_exact_scope(db_session):
    _make_key(db_session, scopes=["reports:billing"], raw="pk2")
    auth = require_user_auth(authorization=None, x_api_key="pk2", db=db_session)
    out = require_permission("reports:billing")(auth=auth, db=db_session)
    assert out["principal_type"] == "api_key"


def test_require_permission_honors_wildcard_scope(db_session):
    _make_key(db_session, scopes=["billing:*"], raw="pk3")
    auth = require_user_auth(authorization=None, x_api_key="pk3", db=db_session)
    out = require_permission("billing:invoice:read")(auth=auth, db=db_session)
    assert out["principal_type"] == "api_key"


def test_require_permission_denies_missing_scope(db_session):
    _make_key(db_session, scopes=["audit:read"], raw="pk4")
    auth = require_user_auth(authorization=None, x_api_key="pk4", db=db_session)
    with pytest.raises(HTTPException) as exc:
        require_permission("network:device:write")(auth=auth, db=db_session)
    assert exc.value.status_code == 403


def test_api_key_principal_is_not_admin(db_session):
    # No roles -> no admin shortcut; a key cannot pass admin-only perms by role.
    _make_key(db_session, scopes=[], raw="pk5")
    auth = require_user_auth(authorization=None, x_api_key="pk5", db=db_session)
    assert "admin" not in auth["roles"]
    with pytest.raises(HTTPException):
        require_permission("system:settings:write")(auth=auth, db=db_session)
