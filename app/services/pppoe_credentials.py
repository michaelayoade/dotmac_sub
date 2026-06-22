"""PPPoE credential auto-generation service.

Generates PPPoE usernames from the subscriber canonical id and random passwords
when a subscription is activated and no active AccessCredential exists.
This always runs on subscription activation — PPPoE credentials are
mandatory for all subscribers.
"""

from __future__ import annotations

import logging
import secrets
import string
from typing import TYPE_CHECKING

from app.models.catalog import AccessCredential, ConnectionType
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import numbering, settings_spec
from app.services.credential_crypto import encrypt_credential
from app.services.customer_identifiers import pppoe_username_from_subscriber_number

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# DocumentSequence key for the fallback username (non-canonical subscribers).
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
        parsed = _resolve_int_setting(
            value, spec.default if isinstance(spec.default, int) else 1
        )
        if spec.min_value is not None and parsed < spec.min_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        if spec.max_value is not None and parsed > spec.max_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        value = parsed
    return value


def _generate_pppoe_username(db: Session, subscriber_id: str) -> str | None:
    from app.models.subscriber import Subscriber

    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is not None:
        derived = pppoe_username_from_subscriber_number(
            db, subscriber.subscriber_number
        )
        if derived:
            return derived
    # Fall back to a sequential username when the subscriber number isn't in the
    # canonical SUB-<digits> shape (imported/manual records). PPPoE credentials
    # are mandatory for activation, so generation must always yield a username —
    # otherwise non-canonical subscribers could never be activated.
    return _generate_pppoe_username_sequence(db)


def _generate_pppoe_username_sequence(db: Session) -> str | None:
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
    """Auto-generate a PPPoE AccessCredential if none exists.

    If the subscriber has no active AccessCredential, generates one with the
    canonical PPPoE username (``10`` + subscriber canonical id) and a random
    encrypted password.

    Args:
        db: Database session.
        subscriber_id: The subscriber UUID.
        radius_profile_id: Optional RADIUS profile to assign.

    Returns:
        The newly created AccessCredential, or None if one already exists.
    """
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

    username = _generate_pppoe_username(db, subscriber_id)
    if not username:
        logger.warning(
            "PPPoE username derivation returned None for subscriber %s",
            subscriber_id,
        )
        return None

    # Generate and encrypt password
    password_length_raw = _resolve_radius_setting(db, "pppoe_default_password_length")
    password_length = _resolve_int_setting(password_length_raw, 12)
    password_length = max(8, min(64, password_length))

    plain_password = _generate_random_password(password_length)
    encrypted_password = encrypt_credential(plain_password)

    credential = (
        db.query(AccessCredential).filter(AccessCredential.username == username).first()
    )
    if credential is not None and str(credential.subscriber_id) != str(subscriber_id):
        raise ValueError(
            f"Derived PPPoE username {username} is already used by another subscriber."
        )

    if credential is None:
        credential = AccessCredential(
            subscriber_id=subscriber_id,
            username=username,
            is_active=True,
            connection_type=ConnectionType.pppoe,
        )
        db.add(credential)

    credential.secret_hash = encrypted_password
    credential.is_active = True
    credential.connection_type = ConnectionType.pppoe
    if radius_profile_id:
        # The column's GUID type coerces str/UUID; assign raw so a malformed
        # profile id doesn't turn into an activation-blocking ValueError here.
        credential.radius_profile_id = radius_profile_id

    db.flush()
    logger.info(
        "Auto-generated PPPoE credential %s for subscriber %s",
        username,
        subscriber_id,
    )
    return credential
