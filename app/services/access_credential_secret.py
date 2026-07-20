"""Canonical representation policy for subscriber access credentials."""

from __future__ import annotations

import re
from enum import StrEnum

_CRYPT_PREFIXES = ("$1$", "$2a$", "$2b$", "$2y$", "$5$", "$6$")
_OPAQUE_VALUE_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


class AccessCredentialSecretFormat(StrEnum):
    empty = "empty"
    encrypted = "encrypted"
    explicit_cleartext = "explicit_cleartext"
    crypt_hash = "crypt_hash"
    pbkdf2_hash = "pbkdf2_hash"
    opaque_hash = "opaque_hash"
    legacy_cleartext = "legacy_cleartext"


_ONE_WAY_FORMATS = frozenset(
    {
        AccessCredentialSecretFormat.crypt_hash,
        AccessCredentialSecretFormat.pbkdf2_hash,
        AccessCredentialSecretFormat.opaque_hash,
    }
)


def classify_access_credential_secret(
    value: str | None,
) -> AccessCredentialSecretFormat:
    """Classify storage semantics without exposing or transforming the value."""
    text = str(value or "").strip()
    if not text:
        return AccessCredentialSecretFormat.empty
    lowered = text.lower()
    if lowered.startswith("enc:"):
        return AccessCredentialSecretFormat.encrypted
    if lowered.startswith(("plain:", "cleartext:")):
        return AccessCredentialSecretFormat.explicit_cleartext
    if text.startswith(_CRYPT_PREFIXES):
        return AccessCredentialSecretFormat.crypt_hash
    if text.startswith("$pbkdf2-"):
        return AccessCredentialSecretFormat.pbkdf2_hash
    if (
        len(text) >= 20
        and _OPAQUE_VALUE_RE.fullmatch(text)
        and any(character in text for character in "+/=")
    ):
        return AccessCredentialSecretFormat.opaque_hash
    return AccessCredentialSecretFormat.legacy_cleartext


def is_one_way_access_credential_secret(value: str | None) -> bool:
    return classify_access_credential_secret(value) in _ONE_WAY_FORMATS


def explicit_cleartext_value(value: str) -> str:
    """Remove a recognized cleartext storage marker."""
    lowered = value.lower()
    if lowered.startswith("plain:"):
        return value[6:]
    if lowered.startswith("cleartext:"):
        return value[10:]
    return value
