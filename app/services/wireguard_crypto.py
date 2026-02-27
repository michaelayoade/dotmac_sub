"""WireGuard cryptographic utilities.

Provides Curve25519 key generation and optional Fernet encryption
for storing private keys at rest.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# Environment variable for the encryption key (should be set in production)
_ENCRYPTION_KEY_ENV = "WIREGUARD_KEY_ENCRYPTION_KEY"
_logger = logging.getLogger(__name__)
_encryption_warning_logged = False


def generate_keypair() -> tuple[str, str]:
    """Generate a WireGuard Curve25519 keypair.

    Returns:
        Tuple of (private_key_base64, public_key_base64)

    WireGuard uses 32-byte Curve25519 keys encoded as base64.
    Key generation is instantaneous (<1ms).
    """
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key()

    # Export keys as raw 32-byte values
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    # Encode as base64 (standard WireGuard format)
    private_b64 = base64.b64encode(private_bytes).decode("ascii")
    public_b64 = base64.b64encode(public_bytes).decode("ascii")

    return private_b64, public_b64


def derive_public_key(private_key_b64: str) -> str:
    """Derive public key from a private key.

    Args:
        private_key_b64: Base64-encoded private key

    Returns:
        Base64-encoded public key
    """
    private_bytes = base64.b64decode(private_key_b64)
    private_key = X25519PrivateKey.from_private_bytes(private_bytes)
    public_key = private_key.public_key()

    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    return base64.b64encode(public_bytes).decode("ascii")


def generate_preshared_key() -> str:
    """Generate a WireGuard preshared key for post-quantum security.

    Returns:
        Base64-encoded 32-byte preshared key
    """
    psk_bytes = secrets.token_bytes(32)
    return base64.b64encode(psk_bytes).decode("ascii")


def validate_key(key_b64: str) -> bool:
    """Validate that a string is a valid WireGuard key format.

    Args:
        key_b64: Base64-encoded key to validate

    Returns:
        True if valid, False otherwise
    """
    try:
        key_bytes = base64.b64decode(key_b64)
        return len(key_bytes) == 32
    except Exception:
        return False


def get_encryption_key() -> bytes:
    """Get the Fernet encryption key from settings or environment.

    Checks in order:
    1. Database settings (wireguard_key_encryption_key in network domain)
    2. Environment variable WIREGUARD_KEY_ENCRYPTION_KEY

    Returns:
        Fernet key bytes

    Raises:
        RuntimeError: If encryption key is not configured
    """
    global _encryption_warning_logged

    key_str: str | None = None

    # Try to get from settings system first
    try:
        from app.db import SessionLocal
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        session = SessionLocal()
        try:
            key_obj = resolve_value(
                session, SettingDomain.network, "wireguard_key_encryption_key"
            )
            if isinstance(key_obj, str):
                key_str = key_obj
        finally:
            session.close()
    except Exception:
        pass  # Fall through to env var

    # Fall back to environment variable
    if not key_str:
        key_str = os.environ.get(_ENCRYPTION_KEY_ENV)

    if not key_str:
        raise RuntimeError(
            "WIREGUARD_KEY_ENCRYPTION_KEY environment variable is required. "
            "Generate one with: make generate-encryption-key"
        )

    try:
        # Key should be URL-safe base64 encoded 32-byte key
        return key_str.encode("ascii")
    except (UnicodeEncodeError, AttributeError) as e:
        raise RuntimeError(
            "WIREGUARD_KEY_ENCRYPTION_KEY must be a valid ASCII string"
        ) from e


def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key.

    Returns:
        URL-safe base64-encoded Fernet key

    Use this to generate a key for the WIREGUARD_KEY_ENCRYPTION_KEY env var.
    """
    return Fernet.generate_key().decode("ascii")


def encrypt_private_key(private_key_b64: str) -> str:
    """Encrypt a private key for storage at rest.

    If no encryption key is configured, raises RuntimeError.

    Args:
        private_key_b64: Base64-encoded private key

    Returns:
        Encrypted key (prefixed with "enc:")

    Raises:
        RuntimeError: If encryption key is not configured
    """
    encryption_key = get_encryption_key()
    fernet = Fernet(encryption_key)
    encrypted = fernet.encrypt(private_key_b64.encode("ascii"))
    return f"enc:{encrypted.decode('ascii')}"


def decrypt_private_key(stored_key: str) -> str:
    """Decrypt a private key from storage.

    Handles both encrypted (enc:) and plain (plain:) formats.

    Args:
        stored_key: Stored key with prefix

    Returns:
        Base64-encoded private key

    Raises:
        ValueError: If decryption fails or format is invalid
    """
    if stored_key.startswith("plain:"):
        return stored_key[6:]

    if stored_key.startswith("enc:"):
        try:
            encryption_key = get_encryption_key()
        except RuntimeError as e:
            raise ValueError(
                "Encrypted key found but WIREGUARD_KEY_ENCRYPTION_KEY not set"
            ) from e
        fernet = Fernet(encryption_key)
        try:
            decrypted = fernet.decrypt(stored_key[4:].encode("ascii"))
            return decrypted.decode("ascii")
        except Exception as e:
            raise ValueError(f"Failed to decrypt private key: {e}") from e

    # Legacy format (no prefix) - treat as plain
    return stored_key


def generate_provision_token() -> str:
    """Generate a secure provisioning token.

    Returns:
        32-character URL-safe token
    """
    return secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    """Hash a token for secure storage.

    Uses SHA-256 for one-way hashing.

    Args:
        token: Plain token

    Returns:
        Hex-encoded hash
    """
    import hashlib

    return hashlib.sha256(token.encode("ascii")).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    """Verify a token against its hash.

    Uses constant-time comparison.

    Args:
        token: Plain token to verify
        token_hash: Stored hash to compare against

    Returns:
        True if token matches hash
    """
    import hmac

    computed_hash = hash_token(token)
    return hmac.compare_digest(computed_hash, token_hash)
