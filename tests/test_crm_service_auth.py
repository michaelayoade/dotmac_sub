"""CRM inbound auth: scoped ApiKey preferred; legacy bearer only during migration.

The static shared bearer (selfcare_api_token) had no scopes, rotation, or
identity yet guarded the money POSTs. require_crm_service_auth accepts a
fail-closed ApiKey holding integration:crm (wildcard-aware) and keeps the
bearer only while settings.crm_legacy_bearer_enabled is true.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings

from app.api.crm import CRM_INTEGRATION_PERMISSION, require_crm_service_auth
from app.models.auth import ApiKey
from app.services.auth import hash_api_key


def _make_key(db, *, scopes, raw="raw-crm-key"):
    key = ApiKey(
        label="crm-service",
        key_hash=hash_api_key(raw),
        scopes=scopes,
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db.add(key)
    db.commit()
    return key, raw


def _request():
    return SimpleNamespace(state=SimpleNamespace(), cookies={})


@pytest.fixture
def crm_settings():
    """Frozen-dataclass-safe settings override, restored after the test."""
    original_token = settings.selfcare_api_token
    original_flag = settings.crm_legacy_bearer_enabled

    def _set(token=None, bearer_enabled=None):
        if token is not None:
            object.__setattr__(settings, "selfcare_api_token", token)
        if bearer_enabled is not None:
            object.__setattr__(settings, "crm_legacy_bearer_enabled", bearer_enabled)

    yield _set
    object.__setattr__(settings, "selfcare_api_token", original_token)
    object.__setattr__(settings, "crm_legacy_bearer_enabled", original_flag)


def _call(db, *, x_api_key=None, authorization=None):
    return require_crm_service_auth(
        request=_request(),
        authorization=authorization,
        x_api_key=x_api_key,
        db=db,
    )


def test_scoped_api_key_is_accepted(db_session):
    _make_key(db_session, scopes=[CRM_INTEGRATION_PERMISSION])
    _call(db_session, x_api_key="raw-crm-key")  # no exception


def test_wildcard_scope_satisfies(db_session):
    _make_key(db_session, scopes=["integration:*"], raw="raw-wild")
    _call(db_session, x_api_key="raw-wild")


def test_key_without_scope_fails_closed(db_session):
    _make_key(db_session, scopes=["billing:read"], raw="raw-wrong")
    with pytest.raises(HTTPException) as exc:
        _call(db_session, x_api_key="raw-wrong")
    assert exc.value.status_code == 401


def test_unknown_key_rejected_without_bearer_fallback(db_session, crm_settings):
    # An X-Api-Key attempt must not fall through to the bearer path.
    crm_settings(token="tok", bearer_enabled=True)
    with pytest.raises(HTTPException) as exc:
        _call(db_session, x_api_key="no-such-key", authorization="Bearer tok")
    assert exc.value.status_code == 401


def test_legacy_bearer_accepted_while_enabled(db_session, crm_settings):
    crm_settings(token="tok", bearer_enabled=True)
    _call(db_session, authorization="Bearer tok")  # no exception


def test_legacy_bearer_rejected_after_cutover(db_session, crm_settings):
    crm_settings(token="tok", bearer_enabled=False)
    with pytest.raises(HTTPException) as exc:
        _call(db_session, authorization="Bearer tok")
    assert exc.value.status_code == 401


def test_no_credentials_rejected(db_session, crm_settings):
    crm_settings(token="tok", bearer_enabled=True)
    with pytest.raises(HTTPException) as exc:
        _call(db_session)
    assert exc.value.status_code == 401
