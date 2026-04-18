"""Rotate credential-at-rest Fernet material without losing stored secrets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import BankAccount, PaymentMethod
from app.models.catalog import AccessCredential, NasDevice
from app.models.domain_settings import DomainSetting
from app.models.integration_hook import IntegrationHook
from app.models.network import OLTDevice, OntProfileWanService, OntUnit
from app.models.network_monitoring import NetworkDevice
from app.models.tr069 import Tr069AcsServer
from app.models.webhook import WebhookEndpoint
from app.services.credential_crypto import (
    ENCRYPTED_MODEL_FIELDS,
    decrypt_credential_with_key,
    encrypt_credential_with_key,
)
from app.services.secrets import clear_cache, read_secret_fields, write_secret
from app.services.settings_cache import SettingsCache

logger = logging.getLogger(__name__)

_INTEGRATION_HOOK_SECRET_KEYS = frozenset(
    {"token", "password", "secret", "api_key", "access_token", "refresh_token"}
)
_CREDENTIAL_KEY_SECRET_PATH = "settings/auth"
_CREDENTIAL_KEY_SECRET_FIELD = "credential_encryption_key"
_LEGACY_CREDENTIAL_KEY_SECRET_PATH = "auth"


@dataclass(frozen=True)
class CredentialKeyRotationResult:
    updated_records: int
    updated_values: int


_MODEL_BY_NAME: dict[str, type[Any]] = {
    "NasDevice": NasDevice,
    "NetworkDevice": NetworkDevice,
    "AccessCredential": AccessCredential,
    "OLTDevice": OLTDevice,
    "OntUnit": OntUnit,
    "OntProfileWanService": OntProfileWanService,
    "Tr069AcsServer": Tr069AcsServer,
    "WebhookEndpoint": WebhookEndpoint,
    "PaymentMethod": PaymentMethod,
    "BankAccount": BankAccount,
}

_MODEL_FIELDS: tuple[tuple[type[Any], tuple[str, ...]], ...] = tuple(
    (model, ENCRYPTED_MODEL_FIELDS[model_name])
    for model_name, model in _MODEL_BY_NAME.items()
)


def _rotate_value(
    value: str | None,
    *,
    old_key: str,
    new_key: str,
) -> tuple[str | None, bool]:
    """Rotate a credential value from old key to new key.

    Returns (rotated_value, changed).

    Handles edge cases:
    - Value already encrypted with new key: returns unchanged
    - Plain/legacy values are encrypted with the new key
    - Corrupted/unrecoverable encrypted values raise ValueError
    """
    if not value:
        return value, False

    if value.startswith("plain:"):
        plain_value = value[6:]
        rotated = encrypt_credential_with_key(plain_value, new_key)
        return rotated, rotated != value

    if not value.startswith("enc:"):
        rotated = encrypt_credential_with_key(value, new_key)
        return rotated, rotated != value

    # Try decrypting with old key first
    try:
        decrypted_plaintext = decrypt_credential_with_key(value, old_key)
        if decrypted_plaintext is None:
            return value, False
    except ValueError:
        # Old key didn't work - check if already encrypted with new key
        try:
            decrypt_credential_with_key(value, new_key)
            # Successfully decrypted with new key - already rotated
            logger.debug("Value already encrypted with new key, skipping")
            return value, False
        except ValueError:
            raise ValueError(
                "Cannot decrypt value with either old or new key"
            ) from None

    # Re-encrypt with new key
    rotated = encrypt_credential_with_key(decrypted_plaintext, new_key)
    if rotated == value:
        return value, False
    return rotated, True


def _record_identity(row: Any) -> str:
    row_id = getattr(row, "id", None)
    return str(row_id) if row_id is not None else "<no-id>"


def _validate_model_fields() -> None:
    for model, fields in _MODEL_FIELDS:
        columns = model.__table__.columns
        for field in fields:
            if field not in columns:
                raise ValueError(f"{model.__name__}.{field} is not a mapped column")


def _rotate_model_fields(
    db: Session,
    model: type[Any],
    fields: tuple[str, ...],
    *,
    old_key: str,
    new_key: str,
) -> tuple[int, int]:
    updated_records = 0
    updated_values = 0
    columns = model.__table__.columns
    column_lengths = {
        field: getattr(columns[field].type, "length", None) for field in fields
    }
    logger.debug("Rotating credential fields for model %s", model.__name__)
    for row in db.scalars(select(model)).all():
        row_changed = False
        for field in fields:
            current = getattr(row, field, None)
            try:
                rotated, changed = _rotate_value(
                    current, old_key=old_key, new_key=new_key
                )
            except ValueError as exc:
                raise ValueError(
                    f"Failed to rotate {model.__name__}.{field} "
                    f"id={_record_identity(row)}"
                ) from exc
            if not changed:
                continue
            max_length = column_lengths.get(field)
            if (
                max_length is not None
                and isinstance(rotated, str)
                and len(rotated) > max_length
            ):
                raise ValueError(
                    f"Rotated value for {model.__name__}.{field} id={_record_identity(row)} "
                    f"exceeds column length {max_length}"
                )
            setattr(row, field, rotated)
            row_changed = True
            updated_values += 1
        if row_changed:
            updated_records += 1
    return updated_records, updated_values


def _rotate_domain_settings(
    db: Session, *, old_key: str, new_key: str
) -> tuple[int, int]:
    updated_records = 0
    updated_values = 0
    rows = list(
        db.scalars(
            select(DomainSetting)
            .where(DomainSetting.value_text.is_not(None))
            .where(DomainSetting.is_active.is_(True))
        ).all()
    )
    for row in rows:
        current = str(row.value_text or "")
        # Note: _rotate_value now checks for enc: prefix internally
        try:
            rotated, changed = _rotate_value(current, old_key=old_key, new_key=new_key)
        except ValueError as exc:
            raise ValueError(
                f"Failed to rotate DomainSetting {row.domain.value}.{row.key}"
            ) from exc
        if not changed:
            continue
        row.value_text = rotated
        SettingsCache.invalidate(row.domain.value, row.key)
        updated_records += 1
        updated_values += 1
    return updated_records, updated_values


def _rotate_integration_hooks(
    db: Session, *, old_key: str, new_key: str
) -> tuple[int, int]:
    updated_records = 0
    updated_values = 0
    for hook in db.scalars(select(IntegrationHook)).all():
        auth_config = hook.auth_config or {}
        if not isinstance(auth_config, dict):
            continue
        changed = False
        rotated = dict(auth_config)
        for key, value in auth_config.items():
            if key not in _INTEGRATION_HOOK_SECRET_KEYS or value is None:
                continue
            try:
                rotated_value, value_changed = _rotate_value(
                    str(value), old_key=old_key, new_key=new_key
                )
            except ValueError as exc:
                raise ValueError(
                    f"Failed to rotate IntegrationHook auth_config.{key}"
                ) from exc
            if not value_changed:
                continue
            rotated[key] = rotated_value
            updated_values += 1
            changed = True
        if changed:
            hook.auth_config = rotated
            updated_records += 1
    return updated_records, updated_values


def rotate_credential_encryption_material(
    db: Session,
    *,
    old_key: str,
    new_key: str,
    commit: bool = True,
) -> CredentialKeyRotationResult:
    """Re-encrypt all known credential-at-rest values with a new Fernet key."""
    _validate_model_fields()
    logger.info("Starting credential encryption material rotation")
    updated_records = 0
    updated_values = 0

    for model, fields in _MODEL_FIELDS:
        records, values = _rotate_model_fields(
            db, model, fields, old_key=old_key, new_key=new_key
        )
        updated_records += records
        updated_values += values

    records, values = _rotate_domain_settings(db, old_key=old_key, new_key=new_key)
    updated_records += records
    updated_values += values

    records, values = _rotate_integration_hooks(db, old_key=old_key, new_key=new_key)
    updated_records += records
    updated_values += values

    if commit:
        db.commit()
    else:
        db.flush()

    logger.info(
        "Finished credential encryption material rotation: records=%d values=%d commit=%s",
        updated_records,
        updated_values,
        commit,
    )
    return CredentialKeyRotationResult(
        updated_records=updated_records,
        updated_values=updated_values,
    )


def update_openbao_credential_encryption_key(new_key: str) -> bool:
    try:
        existing = read_secret_fields(_CREDENTIAL_KEY_SECRET_PATH)
        payload = dict(existing)
        payload[_CREDENTIAL_KEY_SECRET_FIELD] = new_key
        success = write_secret(_CREDENTIAL_KEY_SECRET_PATH, payload)

        legacy_existing = read_secret_fields(_LEGACY_CREDENTIAL_KEY_SECRET_PATH)
        legacy_payload = dict(legacy_existing)
        legacy_payload[_CREDENTIAL_KEY_SECRET_FIELD] = new_key
        success = (
            write_secret(_LEGACY_CREDENTIAL_KEY_SECRET_PATH, legacy_payload) and success
        )
        if success:
            clear_cache()
        return success
    except Exception:
        logger.exception("Failed to update OpenBao credential encryption key")
        return False
