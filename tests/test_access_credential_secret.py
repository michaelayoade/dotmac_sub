from __future__ import annotations

import pytest

from app.services.access_credential_secret import (
    AccessCredentialSecretFormat,
    classify_access_credential_secret,
    explicit_cleartext_value,
    is_one_way_access_credential_secret,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, AccessCredentialSecretFormat.empty),
        ("enc:ciphertext", AccessCredentialSecretFormat.encrypted),
        ("plain:secret", AccessCredentialSecretFormat.explicit_cleartext),
        ("cleartext:secret", AccessCredentialSecretFormat.explicit_cleartext),
        ("$6$salt$digest", AccessCredentialSecretFormat.crypt_hash),
        ("$pbkdf2-sha256$rounds$digest", AccessCredentialSecretFormat.pbkdf2_hash),
        ("YWJjZGVmZ2hpamtsbW5vcA==", AccessCredentialSecretFormat.opaque_hash),
        ("legacy-secret", AccessCredentialSecretFormat.legacy_cleartext),
    ],
)
def test_classify_access_credential_secret(value, expected):
    assert classify_access_credential_secret(value) == expected


@pytest.mark.parametrize(
    "value",
    ["$6$salt$digest", "$pbkdf2-sha256$rounds$digest", "YWJjZGVmZ2hpamtsbW5vcA=="],
)
def test_one_way_access_credentials_are_explicit(value):
    assert is_one_way_access_credential_secret(value)


def test_explicit_cleartext_value_removes_supported_marker():
    assert explicit_cleartext_value("plain:secret") == "secret"
    assert explicit_cleartext_value("cleartext:secret") == "secret"
