"""PPPoE credential auto-generation service.

Generates PPPoE usernames from a DocumentSequence and random passwords
when a subscription is activated and no active AccessCredential exists.
Disabled by default — controlled by the ``pppoe_auto_generate_enabled``
domain setting under ``SettingDomain.radius``.
"""

from __future__ import annotations

import logging
import secrets
import string
from typing import TYPE_CHECKING

from app.models.catalog import AccessCredential
from app.models.domain_settings import SettingDomain
from app.services import numbering, settings_spec
from app.services.credential_crypto import encrypt_credential

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SEQUENCE_KEY = "pppoe_username"


def _generate_random_password(length: int) -> str:
    """Generate a cryptographically random alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _resolve_int_setting(value: object, fallback: int) -> int:
    """Coerce a setting value to int with a fallback."""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return fallback
    return fallback


def auto_generate_pppoe_credential(
    db: Session,
    subscriber_id: str,
    *,
    radius_profile_id: str | None = None,
) -> AccessCredential | None:
    """Auto-generate a PPPoE AccessCredential if enabled and none exists.

    Checks the ``pppoe_auto_generate_enabled`` setting.  If enabled and the
    subscriber has no active AccessCredential, generates one with a
    sequential username (``1000`` prefix + zero-padded sequence) and a
    random encrypted password.

    Args:
        db: Database session.
        subscriber_id: The subscriber UUID.
        radius_profile_id: Optional RADIUS profile to assign.

    Returns:
        The newly created AccessCredential, or None if skipped.
    """
    enabled = settings_spec.resolve_value(
        db, SettingDomain.radius, "pppoe_auto_generate_enabled",
    )
    if enabled is not True:
        return None

    # Check for existing active credentials
    existing = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscriber_id == subscriber_id,
            AccessCredential.is_active.is_(True),
        )
        .first()
    )
    if existing:
        logger.debug(
            "Subscriber %s already has active credential %s, skipping PPPoE auto-gen",
            subscriber_id,
            existing.username,
        )
        return None

    # Generate username via DocumentSequence
    username = numbering.generate_number(
        db,
        SettingDomain.radius,
        SEQUENCE_KEY,
        "pppoe_auto_generate_enabled",
        "pppoe_username_prefix",
        "pppoe_username_padding",
        "pppoe_username_start",
    )
    if not username:
        logger.warning(
            "PPPoE username generation returned None for subscriber %s",
            subscriber_id,
        )
        return None

    # Generate and encrypt password
    password_length_raw = settings_spec.resolve_value(
        db, SettingDomain.radius, "pppoe_default_password_length",
    )
    password_length = _resolve_int_setting(password_length_raw, 12)
    password_length = max(8, min(64, password_length))

    plain_password = _generate_random_password(password_length)
    encrypted_password = encrypt_credential(plain_password)

    credential = AccessCredential(
        subscriber_id=subscriber_id,
        username=username,
        secret_hash=encrypted_password,
        is_active=True,
        radius_profile_id=radius_profile_id,
    )
    db.add(credential)
    db.flush()

    logger.info(
        "Auto-generated PPPoE credential %s for subscriber %s",
        username,
        subscriber_id,
    )
    return credential
