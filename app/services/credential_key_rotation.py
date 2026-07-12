"""Rotate credential-at-rest Fernet material without losing stored secrets."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.billing import BankAccount, PaymentMethod
from app.models.catalog import AccessCredential, NasDevice
from app.models.domain_settings import DomainSetting
from app.models.integration_hook import (
    SECRET_AUTH_CONFIG_KEYS as _INTEGRATION_HOOK_SECRET_KEYS,
)
from app.models.integration_hook import IntegrationHook
from app.models.network import OLTDevice, OntProfileWanService, OntUnit
from app.models.network_monitoring import NetworkDevice
from app.models.router_management import JumpHost, Router
from app.models.system_user import SystemUser
from app.models.tr069 import Tr069AcsServer
from app.models.vas import VasTransaction
from app.models.webhook import WebhookEndpoint
from app.services.credential_crypto import (
    ENCRYPTED_MODEL_FIELDS,
    decrypt_credential_with_key,
    encrypt_credential_with_key,
    get_encryption_key,
    get_previous_encryption_key,
)
from app.services.network.ont_desired_config import (
    desired_config_values_for_paths,
    rotate_desired_config_credentials,
)
from app.services.secrets import is_secret_ref
from app.services.settings_cache import SettingsCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CredentialKeyRotationResult:
    updated_records: int
    updated_values: int


_INTEGRITY_STATES = (
    "encrypted",
    "plaintext",
    "undecryptable",
    "reference",
    "empty",
)


@dataclass(frozen=True)
class CredentialIntegrityResult:
    """Redacted credential inventory suitable for observability publication."""

    counts: dict[str, dict[str, int]]
    totals: dict[str, int]
    scanned_at: datetime

    @property
    def values_scanned(self) -> int:
        return sum(self.totals.values())

    def observations(self) -> list[tuple[str, str, float]]:
        values: list[tuple[str, str, float]] = []
        for scope, states in sorted(self.counts.items()):
            for state in _INTEGRITY_STATES:
                values.append((state, scope, float(states.get(state, 0))))
        for state in _INTEGRITY_STATES:
            values.append((state, "all", float(self.totals.get(state, 0))))
        return values


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
    "VasTransaction": VasTransaction,
    "SystemUser": SystemUser,
    "Router": Router,
    "JumpHost": JumpHost,
}

_MODEL_FIELDS: tuple[tuple[type[Any], tuple[str, ...]], ...] = tuple(
    (model, ENCRYPTED_MODEL_FIELDS[model_name])
    for model_name, model in _MODEL_BY_NAME.items()
)

_ONT_DESIRED_CONFIG_CREDENTIAL_PATHS: tuple[tuple[str, ...], ...] = (
    ("wifi", "password"),
)

_RAW_ENCRYPTED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("ConnectorConfig.headers", "connector_configs", "headers"),
    ("OAuthToken.access_token", "oauth_tokens", "access_token"),
    ("OAuthToken.refresh_token", "oauth_tokens", "refresh_token"),
)


def _empty_integrity_counts() -> dict[str, int]:
    return dict.fromkeys(_INTEGRITY_STATES, 0)


def _credential_state(value: Any, keys: tuple[bytes, ...]) -> str:
    if value is None or value == "":
        return "empty"
    text_value = str(value)
    if is_secret_ref(text_value):
        return "reference"
    if text_value.startswith("plain:") or not text_value.startswith("enc:"):
        return "plaintext"
    for key in keys:
        try:
            decrypt_credential_with_key(text_value, key)
            return "encrypted"
        except ValueError:
            continue
    return "undecryptable"


def scan_credential_encryption_integrity(
    db: Session,
    *,
    now: datetime | None = None,
) -> CredentialIntegrityResult:
    """Classify every rotation-owned value without exposing record identities."""
    _validate_model_fields()
    keys = tuple(
        key
        for key in (get_encryption_key(), get_previous_encryption_key())
        if key is not None
    )
    counts: dict[str, dict[str, int]] = {}

    def register(scope: str) -> None:
        counts.setdefault(scope, _empty_integrity_counts())

    def observe(scope: str, value: Any) -> None:
        register(scope)
        counts[scope][_credential_state(value, keys)] += 1

    for model, fields in _MODEL_FIELDS:
        for field in fields:
            register(f"{model.__name__}.{field}")
        for row in db.scalars(select(model)).all():
            for field in fields:
                observe(f"{model.__name__}.{field}", getattr(row, field, None))

    nested_scopes = {
        path: ".".join(("OntUnit", "desired_config", *path))
        for path in _ONT_DESIRED_CONFIG_CREDENTIAL_PATHS
    }
    for scope in nested_scopes.values():
        register(scope)
    for path, value in desired_config_values_for_paths(
        db, _ONT_DESIRED_CONFIG_CREDENTIAL_PATHS
    ):
        observe(nested_scopes[path], value)

    settings_scope = "DomainSetting.value_text"
    register(settings_scope)
    settings_rows = db.scalars(
        select(DomainSetting)
        .where(DomainSetting.value_text.is_not(None))
        .where(DomainSetting.is_active.is_(True))
        .where(DomainSetting.is_secret.is_(True))
    ).all()
    for row in settings_rows:
        observe(settings_scope, row.value_text)

    hook_scopes = {
        key: f"IntegrationHook.auth_config.{key}"
        for key in sorted(_INTEGRATION_HOOK_SECRET_KEYS)
    }
    for scope in hook_scopes.values():
        register(scope)
    for hook in db.scalars(select(IntegrationHook)).all():
        auth_config = hook.auth_config
        if not isinstance(auth_config, dict):
            continue
        for key, scope in hook_scopes.items():
            if key in auth_config and auth_config[key] is not None:
                observe(scope, auth_config[key])

    connector_scope = "ConnectorConfig.auth_config"
    register(connector_scope)
    connector_rows = db.execute(
        text("SELECT auth_config FROM connector_configs WHERE auth_config IS NOT NULL")
    ).all()
    for row in connector_rows:
        observe(connector_scope, row.auth_config)

    for scope, table_name, column_name in _RAW_ENCRYPTED_COLUMNS:
        register(scope)
        rows = db.execute(
            text(
                f"SELECT {column_name} FROM {table_name} "
                f"WHERE {column_name} IS NOT NULL"
            )
        ).all()
        for row in rows:
            observe(scope, row[0])

    totals = Counter(dict.fromkeys(_INTEGRITY_STATES, 0))
    for states in counts.values():
        totals.update(states)
    return CredentialIntegrityResult(
        counts=counts,
        totals={state: int(totals[state]) for state in _INTEGRITY_STATES},
        scanned_at=(now or datetime.now(UTC)).astimezone(UTC),
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

    from app.services.secrets import is_secret_ref

    if is_secret_ref(value):
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
            if field not in columns and not hasattr(model, field):
                raise ValueError(
                    f"{model.__name__}.{field} is not a mapped column or model attribute"
                )


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
        field: getattr(columns[field].type, "length", None)
        for field in fields
        if field in columns
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


def _rotate_ont_desired_config_credentials(
    db: Session,
    *,
    old_key: str,
    new_key: str,
) -> tuple[int, int]:
    updated_records = 0
    updated_values = 0
    for ont in db.scalars(select(OntUnit)).all():

        def rotate_nested_value(
            path: tuple[str, ...], current: Any
        ) -> tuple[Any, bool]:
            try:
                return _rotate_value(current, old_key=old_key, new_key=new_key)
            except ValueError as exc:
                dotted_path = ".".join(("desired_config", *path))
                raise ValueError(
                    f"Failed to rotate OntUnit.{dotted_path} id={_record_identity(ont)}"  # noqa: B023
                ) from exc

        changed_values = rotate_desired_config_credentials(
            ont,
            _ONT_DESIRED_CONFIG_CREDENTIAL_PATHS,
            rotate_nested_value,
        )
        if changed_values:
            updated_records += 1
            updated_values += changed_values
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
            .where(DomainSetting.is_secret.is_(True))
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


def _rotate_connector_auth_config(
    db: Session, *, old_key: str, new_key: str
) -> tuple[int, int]:
    """Rotate ``ConnectorConfig.auth_config`` (a whole-blob EncryptedJSON column).

    The column is TEXT, so the raw stored value is a plain ``enc:``/``plain:`` (or
    legacy plaintext) string on every dialect. We read/write it via straight SQL —
    not the ORM — so the EncryptedJSON type's ambient-key encode/decode never runs
    and we control the old/new keys explicitly. Legacy plaintext blobs are encrypted
    in passing (``_rotate_value`` encrypts non-``enc:`` input with the new key).
    """
    updated_records = 0
    updated_values = 0
    rows = db.execute(
        text(
            "SELECT id, auth_config FROM connector_configs "
            "WHERE auth_config IS NOT NULL"
        )
    ).all()
    for row in rows:
        raw = row.auth_config
        if not isinstance(raw, str) or not raw:
            continue
        rotated, changed = _rotate_value(raw, old_key=old_key, new_key=new_key)
        if not changed:
            continue
        db.execute(
            text("UPDATE connector_configs SET auth_config = :v WHERE id = :id"),
            {"v": rotated, "id": row.id},
        )
        updated_records += 1
        updated_values += 1
    return updated_records, updated_values


def _rotate_raw_encrypted_columns(
    db: Session, *, old_key: str, new_key: str
) -> tuple[int, int]:
    """Rotate whole-blob/string encrypted columns without ORM decryption."""
    updated_records = 0
    updated_values = 0
    for _scope, table_name, column_name in _RAW_ENCRYPTED_COLUMNS:
        rows = db.execute(
            text(
                f"SELECT id, {column_name} FROM {table_name} "
                f"WHERE {column_name} IS NOT NULL"
            )
        ).mappings()
        for row in rows:
            raw = row[column_name]
            if not isinstance(raw, str) or not raw:
                continue
            rotated, changed = _rotate_value(raw, old_key=old_key, new_key=new_key)
            if not changed:
                continue
            db.execute(
                text(
                    f"UPDATE {table_name} SET {column_name} = :value WHERE id = :id"
                ),
                {"value": rotated, "id": row["id"]},
            )
            updated_records += 1
            updated_values += 1
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

    records, values = _rotate_ont_desired_config_credentials(
        db, old_key=old_key, new_key=new_key
    )
    updated_records += records
    updated_values += values

    records, values = _rotate_domain_settings(db, old_key=old_key, new_key=new_key)
    updated_records += records
    updated_values += values

    records, values = _rotate_integration_hooks(db, old_key=old_key, new_key=new_key)
    updated_records += records
    updated_values += values

    records, values = _rotate_connector_auth_config(
        db, old_key=old_key, new_key=new_key
    )
    updated_records += records
    updated_values += values

    records, values = _rotate_raw_encrypted_columns(
        db, old_key=old_key, new_key=new_key
    )
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
