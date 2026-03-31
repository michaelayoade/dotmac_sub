"""Credential encryption utilities.

Provides Fernet encryption for storing sensitive credentials at rest.
Follows the same pattern as wireguard_crypto.py for consistency.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

# Environment variable for the encryption key
_ENCRYPTION_KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY"
_logger = logging.getLogger(__name__)
_encryption_warning_logged = False

ENCRYPTED_MODEL_FIELDS: dict[str, tuple[str, ...]] = {
    "NasDevice": (
        "shared_secret",
        "ssh_password",
        "ssh_key",
        "api_password",
        "api_token",
        "snmp_community",
    ),
    "NetworkDevice": (
        "snmp_community",
        "snmp_rw_community",
        "snmp_auth_secret",
        "snmp_priv_secret",
    ),
    "AccessCredential": ("secret_hash",),
    "OLTDevice": ("ssh_password", "snmp_ro_community", "snmp_rw_community"),
    "OntUnit": ("pppoe_password",),
    "OntProfileWanService": ("pppoe_static_password",),
    "Tr069AcsServer": ("cwmp_password", "connection_request_password"),
    "WebhookEndpoint": ("secret",),
    "PaymentMethod": ("token",),
    "BankAccount": ("token",),
}


def get_encryption_key() -> bytes | None:
    """Get the Fernet encryption key from settings or environment.

    Checks in order:
    1. Database settings (credential_encryption_key in security domain)
    2. Environment variable CREDENTIAL_ENCRYPTION_KEY

    Returns:
        Fernet key bytes if set, None otherwise
    """
    global _encryption_warning_logged

    key_str: str | bytes | None = None

    # Try OpenBao first, then settings DB, then env var
    try:
        from app.services.secrets import get_secret

        bao_val = get_secret("auth", "credential_encryption_key")
        if bao_val:
            key_str = bao_val
    except Exception:
        _logger.debug("OpenBao credential encryption key lookup failed", exc_info=True)

    if not key_str:
        try:
            from app.db import SessionLocal
            from app.models.domain_settings import SettingDomain
            from app.services.settings_spec import resolve_value

            session = SessionLocal()
            try:
                raw = resolve_value(
                    session, SettingDomain.auth, "credential_encryption_key"
                )
                if isinstance(raw, str):
                    key_str = raw
            finally:
                session.close()
        except Exception:
            _logger.debug(
                "Database credential encryption key lookup failed",
                exc_info=True,
            )

    # Fall back to environment variable
    if not key_str:
        key_str = os.environ.get(_ENCRYPTION_KEY_ENV)

    if not key_str:
        if not _encryption_warning_logged:
            _logger.warning(
                "CREDENTIAL_ENCRYPTION_KEY not configured. "
                "NAS device credentials will be stored unencrypted."
            )
            _encryption_warning_logged = True
        return None

    if isinstance(key_str, bytes):
        return key_str
    if not isinstance(key_str, str):
        return None
    # Key should be URL-safe base64 encoded 32-byte key
    return key_str.encode("ascii")


def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key.

    Returns:
        URL-safe base64-encoded Fernet key

    Use this to generate a key for the CREDENTIAL_ENCRYPTION_KEY env var:
        python -c "from app.services.credential_crypto import generate_encryption_key; print(generate_encryption_key())"
    """
    return Fernet.generate_key().decode("ascii")


def _coerce_encryption_key(encryption_key: str | bytes | None) -> bytes | None:
    if not encryption_key:
        return None
    if isinstance(encryption_key, bytes):
        return encryption_key
    if not isinstance(encryption_key, str):
        raise TypeError("encryption_key must be a str, bytes, or None")
    try:
        return encryption_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("encryption_key must be ASCII-safe Fernet text") from exc


def is_encrypted(value: str | None) -> bool:
    """Check if a value is already encrypted.

    Args:
        value: The value to check

    Returns:
        True if the value has 'enc:' or 'plain:' prefix
    """
    if not value:
        return False
    return value.startswith(("enc:", "plain:"))


def encrypt_credential(value: str | None) -> str | None:
    """Encrypt a credential for storage at rest.

    If no encryption key is configured, returns the credential unchanged
    but prefixed with "plain:" for identification.

    Args:
        value: Plain credential value to encrypt

    Returns:
        Encrypted credential (prefixed with "enc:") or plain credential
        (prefixed with "plain:"), or None if input is None/empty
    """
    if not value:
        return value

    # Don't double-encrypt
    if is_encrypted(value):
        return value

    return encrypt_credential_with_key(value, get_encryption_key())


def encrypt_credential_with_key(
    value: str | None, encryption_key: str | bytes | None
) -> str | None:
    """Encrypt a credential using an explicit Fernet key.

    If ``encryption_key`` is absent, the value is stored with a ``plain:`` prefix.
    """
    if not value:
        return value
    if is_encrypted(value):
        return value

    key_bytes = _coerce_encryption_key(encryption_key)
    if not key_bytes:
        _logger.warning(
            "CREDENTIAL_ENCRYPTION_KEY not configured — storing credential as plaintext"
        )
        return f"plain:{value}"

    fernet = Fernet(key_bytes)
    encrypted = fernet.encrypt(value.encode("utf-8"))
    return f"enc:{encrypted.decode('ascii')}"


def decrypt_credential(value: str | None) -> str | None:
    """Decrypt a credential from storage.

    Handles encrypted (enc:), plain (plain:), and legacy (no prefix) formats.

    Args:
        value: Stored credential with prefix

    Returns:
        Decrypted/plain credential value, or None if input is None/empty

    Raises:
        ValueError: If decryption fails
    """
    if not value:
        return value

    if value.startswith("plain:"):
        return value[6:]

    if value.startswith("enc:"):
        return decrypt_credential_with_key(value, get_encryption_key())

    # Legacy format (no prefix) - treat as plain
    return value


def decrypt_credential_with_key(
    value: str | None, encryption_key: str | bytes | None
) -> str | None:
    """Decrypt a credential using an explicit Fernet key.

    Legacy values without an ``enc:`` or ``plain:`` prefix are treated as plaintext.
    """
    if not value:
        return value

    if value.startswith("plain:"):
        return value[6:]

    if value.startswith("enc:"):
        key_bytes = _coerce_encryption_key(encryption_key)
        if not key_bytes:
            raise ValueError(
                "Encrypted credential found but CREDENTIAL_ENCRYPTION_KEY not set"
            )
        fernet = Fernet(key_bytes)
        try:
            decrypted = fernet.decrypt(value[4:].encode("ascii"))
            return decrypted.decode("utf-8")
        except InvalidToken as e:
            raise ValueError("Failed to decrypt credential: invalid token") from e
        except Exception as e:
            raise ValueError(f"Failed to decrypt credential: {e}") from e

    return value


# Credential field names that should be encrypted
ENCRYPTED_CREDENTIAL_FIELDS = frozenset(
    field for fields in ENCRYPTED_MODEL_FIELDS.values() for field in fields
)
ENCRYPTED_NAS_CREDENTIAL_FIELDS = frozenset(ENCRYPTED_MODEL_FIELDS["NasDevice"])


def encrypt_nas_credentials(data: dict) -> dict:
    """Encrypt all credential fields in a NAS device data dict.

    Args:
        data: Dictionary containing NAS device fields

    Returns:
        Dictionary with credential fields encrypted
    """
    result = dict(data)
    for field in ENCRYPTED_NAS_CREDENTIAL_FIELDS:
        if field in result and result[field]:
            result[field] = encrypt_credential(result[field])
    return result
