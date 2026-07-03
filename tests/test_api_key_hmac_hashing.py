"""API-key hashing: HMAC-with-key + transparent upgrade of legacy sha256 rows."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.models.auth import ApiKey
from app.services import auth as auth_service
from app.services.auth import (
    hash_api_key,
    hash_api_key_candidates,
    is_legacy_api_key_hash,
)
from app.services.auth_dependencies import require_user_auth

_TEST_SECRET = b"0" * 44  # stand-in Fernet-length key


@pytest.fixture
def _with_hmac_secret(monkeypatch):
    # Force a stable server secret so hash_api_key uses the HMAC scheme.
    monkeypatch.setattr(
        "app.services.credential_crypto.get_encryption_key", lambda: _TEST_SECRET
    )


def _legacy(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_hash_uses_hmac_scheme_when_secret_present(_with_hmac_secret):
    h = hash_api_key("secret-key")
    assert h.startswith("hmac256:")
    assert h != _legacy("secret-key")
    assert not is_legacy_api_key_hash(h)


def test_hash_falls_back_to_legacy_without_secret(monkeypatch):
    monkeypatch.setattr(
        "app.services.credential_crypto.get_encryption_key", lambda: None
    )
    assert hash_api_key("secret-key") == _legacy("secret-key")


def test_candidates_include_hmac_and_legacy(_with_hmac_secret):
    cands = hash_api_key_candidates("secret-key")
    assert cands[0].startswith("hmac256:")
    assert _legacy("secret-key") in cands


def test_legacy_stored_key_still_authenticates_and_upgrades(
    _with_hmac_secret, db_session
):
    # A key stored under the pre-HMAC scheme (raw sha256).
    db_session.add(
        ApiKey(
            label="legacy",
            key_hash=_legacy("legacy-raw"),
            scopes=["audit:read"],
            is_active=True,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db_session.commit()

    auth = require_user_auth(authorization=None, x_api_key="legacy-raw", db=db_session)
    assert auth["principal_type"] == "api_key"

    # Re-read: the stored hash must have been upgraded to the HMAC scheme.
    row = db_session.query(ApiKey).filter_by(label="legacy").one()
    assert row.key_hash.startswith("hmac256:")
    assert not is_legacy_api_key_hash(row.key_hash)
    # And it still authenticates under the new hash.
    auth2 = require_user_auth(authorization=None, x_api_key="legacy-raw", db=db_session)
    assert auth2["principal_type"] == "api_key"


def test_new_hmac_key_authenticates(_with_hmac_secret, db_session):
    db_session.add(
        ApiKey(
            label="modern",
            key_hash=hash_api_key("modern-raw"),
            scopes=["audit:read"],
            is_active=True,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db_session.commit()
    auth = require_user_auth(authorization=None, x_api_key="modern-raw", db=db_session)
    assert auth["principal_type"] == "api_key"


def test_hmac_secret_is_derived_not_raw_key(_with_hmac_secret):
    # The stored digest must not be a plain HMAC with the raw Fernet key; it is
    # keyed with a derived subkey, so the two purposes stay independent.
    import hmac

    raw_key_hmac = hmac.new(_TEST_SECRET, b"x", hashlib.sha256).hexdigest()
    got = hash_api_key("x")
    assert got != f"hmac256:{raw_key_hmac}"
    assert (
        got
        == f"hmac256:{hmac.new(auth_service._api_key_hmac_secret(), b'x', hashlib.sha256).hexdigest()}"
    )
