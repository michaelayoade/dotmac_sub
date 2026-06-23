"""Tests for SystemUser.device_login_secret encryption."""

from app.services.credential_crypto import (
    ENCRYPTED_MODEL_FIELDS,
    encrypt_credential,
    decrypt_credential,
)


def test_system_user_secret_registered():
    """Verify SystemUser is registered in ENCRYPTED_MODEL_FIELDS."""
    assert ENCRYPTED_MODEL_FIELDS.get("SystemUser") == ("device_login_secret",)


def test_secret_roundtrip():
    """Verify encrypt/decrypt roundtrip preserves plaintext."""
    plaintext = "Tr0ub4dor"
    enc = encrypt_credential(plaintext)
    assert enc != plaintext  # stored value is not plaintext
    assert decrypt_credential(enc) == plaintext
