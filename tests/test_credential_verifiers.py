"""Verification domains preserve compatibility without writing credentials."""

from __future__ import annotations

import bcrypt
import pytest
from cryptography.fernet import Fernet
from passlib.hash import pbkdf2_sha256, sha512_crypt

from app.metrics import CREDENTIAL_FORMAT_REJECTIONS
from app.services import auth_flow
from app.services.credential_crypto import encrypt_credential_with_key


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "unprefixed",
        "$1$bad",
        "$5$bad",
        "$argon2i$bad",
        "$argon2id$bad",
        "pbkdf2_sha256$bad",
        "$pbkdf2-sha256$bad",
        "$6$bad",
        "enc:bad",
    ],
)
def test_invalid_material_fails_closed(stored: str | None) -> None:
    assert auth_flow.verify_user_password("pw", stored) is False
    assert auth_flow.verify_service_secret("pw", stored) is False


@pytest.mark.parametrize("scheme", [pbkdf2_sha256, sha512_crypt])
def test_legacy_hashes_preserved(scheme: type) -> None:
    stored = scheme.using(rounds=5000).hash("pw")
    for verifier in (auth_flow.verify_user_password, auth_flow.verify_service_secret):
        assert verifier("pw", stored) is True
        assert verifier("incorrect", stored) is False


def test_service_refuses_bcrypt_while_user_accepts_it() -> None:
    stored = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode()
    assert auth_flow.verify_user_password("pw", stored) is True
    assert auth_flow.verify_service_secret("pw", stored) is False


def test_reversible_compatibility_retained_until_recovery_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key.decode())
    encrypted = encrypt_credential_with_key("pw", key)
    for stored in ("plain:pw", encrypted):
        for verifier in (
            auth_flow.verify_user_password,
            auth_flow.verify_service_secret,
        ):
            assert verifier("pw", stored) is True
            assert verifier("incorrect", stored) is False


def test_stored_secret_reference_is_refused_not_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3: a stored reference is never dereferenced to verify a password."""
    from app.services import secrets

    def forbidden(value: str) -> str:
        pytest.fail("a stored reference was resolved")

    monkeypatch.setattr(secrets, "resolve_secret", forbidden)
    monkeypatch.setattr(secrets, "resolve_openbao_ref", forbidden)
    for verifier in (auth_flow.verify_user_password, auth_flow.verify_service_secret):
        for stored in ("bao://secret/test#value", "env://SOME_APP_VAR"):
            assert verifier("pw", stored) is False


def test_decryption_failure_is_false_and_telemetry_has_no_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(value: str | None) -> str | None:
        raise ValueError("synthetic decryption failure")

    monkeypatch.setattr(auth_flow, "decrypt_credential", unavailable)
    before = CREDENTIAL_FORMAT_REJECTIONS._value.get()
    assert auth_flow.verify_user_password("pw", "enc:synthetic") is False
    assert CREDENTIAL_FORMAT_REJECTIONS._value.get() == before + 1
    assert CREDENTIAL_FORMAT_REJECTIONS._labelnames == ()
    assert auth_flow.verify_service_secret("pw", "enc:synthetic") is False


def test_domains_do_not_delegate_to_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(password: str, stored: str | None) -> bool:
        pytest.fail("verification crossed credential domains")

    with monkeypatch.context() as patch:
        patch.setattr(auth_flow, "verify_user_password", forbidden)
        assert auth_flow.verify_service_secret("pw", "plain:pw") is True
    with monkeypatch.context() as patch:
        patch.setattr(auth_flow, "verify_service_secret", forbidden)
        assert auth_flow.verify_user_password("pw", "plain:pw") is True


@pytest.mark.parametrize("password", ["synthetic-π", "synthetic-\ud800"])
def test_non_ascii_reversible_material_returns_denial(password: str) -> None:
    # compare_digest(str, str) rejects non-ASCII values; preserve the existing
    # acceptance boundary while converting its exception into a denial.
    for verifier in (auth_flow.verify_user_password, auth_flow.verify_service_secret):
        assert verifier(password, "plain:" + password) is False


def test_unicode_user_password_still_verifies_supported_hash() -> None:
    stored = pbkdf2_sha256.using(rounds=5000).hash("synthetic-π")
    assert auth_flow.verify_user_password("synthetic-π", stored) is True
    assert auth_flow.verify_user_password("wrong-π", stored) is False
