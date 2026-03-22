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

from app.models.domain_settings import DomainSetting
from app.models.catalog import AccessCredential, ConnectionType
from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
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


def _resolve_radius_setting(db: Session, key: str) -> object | None:
    """Resolve a radius setting directly from the database for activation-time consistency."""
    spec = settings_spec.get_spec(SettingDomain.radius, key)
    if not spec:
        return None

    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.radius)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    raw = settings_spec.extract_db_value(setting)
    if raw is None:
        raw = spec.default

    value, error = settings_spec.coerce_value(spec, raw)
    if error:
        value = spec.default
    if spec.allowed and value is not None and value not in spec.allowed:
        value = spec.default
    if spec.value_type == SettingValueType.integer and value is not None:
        parsed = _resolve_int_setting(value, spec.default if isinstance(spec.default, int) else 1)
        if spec.min_value is not None and parsed < spec.min_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        if spec.max_value is not None and parsed > spec.max_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        value = parsed
    return value


def _generate_pppoe_username(db: Session) -> str | None:
    prefix_value = _resolve_radius_setting(db, "pppoe_username_prefix")
    padding_value = _resolve_radius_setting(db, "pppoe_username_padding")
    start_value = _resolve_radius_setting(db, "pppoe_username_start")
    return numbering.generate_number_with_config(
        db,
        SEQUENCE_KEY,
        prefix=prefix_value if isinstance(prefix_value, str) else None,
        padding=_resolve_int_setting(padding_value, 5),
        start_value=_resolve_int_setting(start_value, 1),
    )


def auto_generate_pppoe_credential(
    db: Session,
    subscriber_id: str,
    *,
    radius_profile_id: str | None = None,
) -> AccessCredential | None:
    """Auto-generate a PPPoE AccessCredential if enabled and none exists.

    Checks the ``pppoe_auto_generate_enabled`` setting.  If enabled and the
    subscriber has no active AccessCredential, generates one with a
    sequential username (``1050`` prefix + zero-padded sequence) and a
    random encrypted password.

    Args:
        db: Database session.
        subscriber_id: The subscriber UUID.
        radius_profile_id: Optional RADIUS profile to assign.

    Returns:
        The newly created AccessCredential, or None if skipped.
    """
    enabled = _resolve_radius_setting(db, "pppoe_auto_generate_enabled")
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
    username = _generate_pppoe_username(db)
    if not username:
        logger.warning(
            "PPPoE username generation returned None for subscriber %s",
            subscriber_id,
        )
        return None

    # Generate and encrypt password
    password_length_raw = _resolve_radius_setting(db, "pppoe_default_password_length")
    password_length = _resolve_int_setting(password_length_raw, 12)
    password_length = max(8, min(64, password_length))

    plain_password = _generate_random_password(password_length)
    encrypted_password = encrypt_credential(plain_password)

    # Retry loop: handle username collisions from migrated data
    max_retries = 5
    for attempt in range(max_retries):
        credential = AccessCredential(
            subscriber_id=subscriber_id,
            username=username,
            secret_hash=encrypted_password,
            is_active=True,
            radius_profile_id=radius_profile_id,
            connection_type=ConnectionType.pppoe,
        )
        db.add(credential)
        try:
            db.flush()
            logger.info(
                "Auto-generated PPPoE credential %s for subscriber %s",
                username,
                subscriber_id,
            )
            return credential
        except Exception as exc:
            db.rollback()
            if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
                raise
            logger.warning(
                "PPPoE username %s already exists (attempt %d/%d), generating next",
                username,
                attempt + 1,
                max_retries,
            )
            # Generate a new username for the next attempt
            username = _generate_pppoe_username(db)
            if not username:
                logger.error("PPPoE username generation exhausted for subscriber %s", subscriber_id)
                return None

    logger.error(
        "Failed to generate unique PPPoE username after %d attempts for subscriber %s",
        max_retries,
        subscriber_id,
    )
    return None
