"""Legacy bcrypt verification (bcrypt>=5 rejects >72-byte secrets; passlib's
bcrypt backend self-test trips on that, so bcrypt hashes bypass passlib)."""

import logging

import bcrypt
import pytest
from passlib.hash import pbkdf2_sha256, sha512_crypt
from starlette.requests import Request

from app.models.auth import AuthProvider, UserCredential
from app.services.auth_flow import AuthFlow, hash_password, verify_password


def _bcrypt_hash(password: str, prefix: str = "2b") -> str:
    raw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode()
    return f"${prefix}${raw[4:]}"


@pytest.mark.parametrize("prefix", ["2a", "2b", "2y"])
def test_supported_prefixes_verify(prefix):
    stored = _bcrypt_hash("correct horse", prefix)
    assert stored.startswith(f"${prefix}$")
    assert verify_password("correct horse", stored) is True
    assert verify_password("wrong horse", stored) is False


def test_2x_prefix_is_not_verifiable():
    assert verify_password("pw", _bcrypt_hash("pw", "2x")) is False


@pytest.mark.parametrize(
    "bad",
    [
        "$2b$",
        "$2b$04$abc",
        "$2b$04$",
        "$2b$99$" + "A" * 53,
        "$2b$04$" + "!" * 53,
        "$2y$garbage",
        "$2a$04$" + "A" * 10,
    ],
)
def test_malformed_hash_is_false_not_error(bad):
    assert verify_password("pw", bad) is False


def test_truncated_real_hash_is_false():
    stored = _bcrypt_hash("pw")
    assert verify_password("pw", stored[:-10]) is False


def test_exactly_72_bytes_ascii_verifies():
    password = "a" * 72
    assert verify_password(password, _bcrypt_hash(password)) is True


def test_73_bytes_fails_closed_and_logs(caplog):
    stored = _bcrypt_hash("a" * 72)
    with caplog.at_level(logging.WARNING, logger="app.services.auth_flow"):
        assert verify_password("a" * 73, stored) is False
    records = [
        r
        for r in caplog.records
        if getattr(r, "event", "") == "auth.bcrypt_password_too_long"
    ]
    assert len(records) == 1
    rendered = repr(records[0].__dict__)
    assert "a" * 20 not in rendered
    assert stored not in rendered
    assert stored[7:] not in rendered


def test_multibyte_over_72_bytes_but_under_72_chars_fails():
    password = "é" * 40  # 40 characters, 80 bytes
    assert len(password) < 72 and len(password.encode("utf-8")) > 72
    assert verify_password(password, _bcrypt_hash("é" * 36)) is False


def test_unicode_under_72_bytes_verifies():
    password = "pässwörd-日本語"
    assert len(password.encode("utf-8")) <= 72
    assert verify_password(password, _bcrypt_hash(password)) is True


def test_other_schemes_unchanged():
    assert verify_password("pw", pbkdf2_sha256.hash("pw")) is True
    assert verify_password("nope", pbkdf2_sha256.hash("pw")) is False
    assert verify_password("pw", sha512_crypt.using(rounds=5000).hash("pw")) is True
    assert verify_password("nope", sha512_crypt.hash("pw")) is False
    assert verify_password("pw", hash_password("pw")) is True


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_login_with_legacy_bcrypt_hash_does_not_raise(db_session, person):
    db_session.add(
        UserCredential(
            person_id=person.id,
            provider=AuthProvider.local,
            username="legacy@example.com",
            password_hash=_bcrypt_hash("secret"),
            is_active=True,
        )
    )
    db_session.commit()

    tokens = AuthFlow.login(
        db_session, "legacy@example.com", "secret", _request(), None
    )
    assert tokens["access_token"]


def test_passlib_context_does_not_include_bcrypt():
    from app.services.auth_flow import PASSWORD_CONTEXT

    assert "bcrypt" not in PASSWORD_CONTEXT.schemes()


def test_login_wrong_password_with_legacy_bcrypt_is_401(db_session, person):
    from fastapi import HTTPException

    db_session.add(
        UserCredential(
            person_id=person.id,
            provider=AuthProvider.local,
            username="legacy2@example.com",
            password_hash=_bcrypt_hash("secret"),
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "legacy2@example.com", "nope", _request(), None)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("prefix", ["2a", "2b", "2y"])
def test_access_credential_login_refuses_bcrypt_secret_hash(
    db_session, person, monkeypatch, caplog, prefix
):
    from fastapi import HTTPException

    from app.models.catalog import AccessCredential

    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = AccessCredential(
        subscriber_id=person.id,
        username=f"pppoe-bcrypt-{prefix}",
        secret_hash=_bcrypt_hash("pppoe-secret", prefix),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    with caplog.at_level(logging.INFO, logger="app.services.auth_flow"):
        with pytest.raises(HTTPException) as exc:
            AuthFlow.login(
                db_session,
                credential.username,
                "pppoe-secret",
                _request(),
                None,
            )
    assert exc.value.status_code == 401
    assert any(
        "stored PPPoE secret unavailable" in r.getMessage() for r in caplog.records
    )
