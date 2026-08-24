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


def test_legacy_reports_scope_remains_read_only_during_granular_migration(db_session):
    _make_key(db_session, scopes=["reports:billing"], raw="pk2")
    auth = require_user_auth(authorization=None, x_api_key="pk2", db=db_session)
    out = require_permission("reports:billing:read")(auth=auth, db=db_session)
    assert out["principal_type"] == "api_key"
    with pytest.raises(HTTPException) as exc:
        require_permission("reports:billing:export")(auth=auth, db=db_session)
    assert exc.value.status_code == 403


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


def test_machine_credential_wins_over_legacy(db_session, monkeypatch):
    """Both tables are read during the migration; the kernel's wins.

    Ordering is the whole safety property. If the legacy table were consulted
    first, a reissued credential would keep resolving to the old row for as
    long as that row existed, and the cutover would silently not happen — the
    traffic would look migrated while still authenticating the old way.
    """

    import app.services.auth_dependencies as deps

    class _Principal:
        credential_id = "11111111-1111-1111-1111-111111111111"
        scopes = frozenset({"billing:invoice:read"})

    monkeypatch.setattr(
        "dotmac_kernel.machine_auth.authenticate_machine",
        lambda db, raw_key, **_: _Principal(),
    )

    auth = deps._api_key_principal(db_session, "whatever", None)

    assert auth is not None
    assert auth["principal_id"] == _Principal.credential_id
    assert auth["scopes"] == ["billing:invoice:read"]
    # A machine principal is not a person. The legacy branch falls back to the
    # key's own id here, which meant two different things depending on how the
    # row was made.
    assert auth["subscriber_id"] is None
    assert auth["person_id"] is None


def test_unknown_machine_key_falls_through(db_session, monkeypatch):
    """Until both credentials are reissued, the old rows must keep working."""

    from dotmac_kernel.exceptions import UnauthorizedError

    import app.services.auth_dependencies as deps

    monkeypatch.setattr(
        "dotmac_kernel.machine_auth.authenticate_machine",
        lambda db, raw_key, **_: (_ for _ in ()).throw(UnauthorizedError("no")),
    )
    _make_key(db_session, scopes=["billing:invoice:read"], raw="legacy-raw-key")

    auth = deps._api_key_principal(db_session, "legacy-raw-key", None)

    assert auth is not None
    assert auth["scopes"] == ["billing:invoice:read"]


def test_machine_auth_is_skipped_when_no_hmac_key_is_held(db_session, monkeypatch):
    """The legacy path must not depend on the new scheme being configured.

    A first version let `MachineKeyUnavailableError` propagate, so that a
    missing key could never be mistaken for a bad credential. That broke 36
    tests across three files: with no secret source installed, every legacy
    credential became a hard error. The migration window's honest reading of an
    absent key is "machine auth is not configured yet", and the legacy rows go
    on working exactly as before.

    The stronger property is deferred, not dropped: once the legacy branch is
    deleted with the last reissued credential, `authenticate_machine` is
    reached unconditionally and the kernel's own error surfaces.
    """

    import app.services.auth_dependencies as deps

    monkeypatch.setattr("dotmac_kernel.secret_sources.get_secret", lambda name: None)
    called = False

    def _never(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("machine auth attempted without a held key")

    monkeypatch.setattr("dotmac_kernel.machine_auth.authenticate_machine", _never)
    _make_key(db_session, scopes=["billing:invoice:read"], raw="legacy-raw-key")

    auth = deps._api_key_principal(db_session, "legacy-raw-key", None)

    assert auth is not None
    assert auth["scopes"] == ["billing:invoice:read"]
    assert called is False
