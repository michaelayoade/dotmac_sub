"""Scheduled lifecycle for credential-at-rest encryption keys."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.credential_crypto import (
    generate_encryption_key,
    get_encryption_key,
)
from app.services.credential_key_rotation import rotate_credential_encryption_material
from app.services.db_session_adapter import db_session_adapter
from app.services.secrets import (
    clear_cache,
    is_openbao_available,
    is_openbao_ref,
    read_secret_fields,
    write_secret,
)
from app.services.settings_spec import read_stored_value, resolve_value

logger = logging.getLogger(__name__)

_ROTATION_LOCK_KEY = 0x43524544  # CRED
_CANONICAL_PATH = "settings/auth"
_LEGACY_PATH = "auth"
_CURRENT_FIELD = "credential_encryption_key"
_PREVIOUS_FIELD = "credential_encryption_previous_key"
_ROTATED_AT_FIELD = "credential_encryption_rotated_at"
_RETIRE_AFTER_FIELD = "credential_encryption_previous_retire_after"


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value)) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _managed_key_source(db: Session) -> tuple[bool, str]:
    env_value = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if env_value:
        if is_openbao_ref(env_value):
            return True, "openbao_env_ref"
        return False, "static_environment_key"

    setting_value = read_stored_value(
        db,
        SettingDomain.auth,
        "credential_encryption_key",
    )
    if setting_value and is_openbao_ref(str(setting_value)):
        return True, "openbao_setting_ref"
    return False, "credential_key_is_not_an_openbao_reference"


def _keyring_payload() -> dict[str, str]:
    canonical = read_secret_fields(_CANONICAL_PATH)
    if canonical.get(_CURRENT_FIELD):
        return dict(canonical)
    return dict(read_secret_fields(_LEGACY_PATH))


def _write_keyring(payload: dict[str, str]) -> bool:
    # Update the legacy path first. If the canonical write then fails, no data
    # rotation has started and both key locations still contain a usable keyring.
    legacy = dict(read_secret_fields(_LEGACY_PATH))
    legacy.update(payload)
    if not write_secret(_LEGACY_PATH, legacy):
        return False
    canonical = dict(read_secret_fields(_CANONICAL_PATH))
    canonical.update(payload)
    return write_secret(_CANONICAL_PATH, canonical)


def _retire_previous_key(payload: dict[str, str]) -> bool:
    retired = dict(payload)
    retired.pop(_PREVIOUS_FIELD, None)
    retired.pop(_RETIRE_AFTER_FIELD, None)

    legacy = dict(read_secret_fields(_LEGACY_PATH))
    legacy.pop(_PREVIOUS_FIELD, None)
    legacy.pop(_RETIRE_AFTER_FIELD, None)
    legacy.update(retired)
    if not write_secret(_LEGACY_PATH, legacy):
        return False

    canonical = dict(read_secret_fields(_CANONICAL_PATH))
    canonical.pop(_PREVIOUS_FIELD, None)
    canonical.pop(_RETIRE_AFTER_FIELD, None)
    canonical.update(retired)
    return write_secret(_CANONICAL_PATH, canonical)


def evaluate_scheduled_rotation(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Evaluate and, when due, safely rotate the managed credential key."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    enabled = _as_bool(
        resolve_value(db, SettingDomain.auth, "credential_rotation_enabled"),
        True,
    )
    if not enabled:
        return {"status": "disabled", "rotated": False}

    interval_days = _as_int(
        resolve_value(db, SettingDomain.auth, "credential_rotation_interval_days"),
        90,
        minimum=7,
        maximum=365,
    )
    grace_days = _as_int(
        resolve_value(db, SettingDomain.auth, "credential_rotation_grace_days"),
        7,
        minimum=1,
        maximum=30,
    )
    auto_apply = _as_bool(
        resolve_value(db, SettingDomain.auth, "credential_rotation_auto_apply"),
        True,
    )

    managed, source = _managed_key_source(db)
    if not managed:
        logger.error("credential_rotation_blocked: source=%s", source)
        return {
            "status": "blocked",
            "reason": source,
            "rotated": False,
        }
    if not is_openbao_available():
        return {
            "status": "blocked",
            "reason": "openbao_unavailable",
            "rotated": False,
        }

    payload = _keyring_payload()
    current_key = str(payload.get(_CURRENT_FIELD) or "")
    active_key = get_encryption_key()
    if not current_key or not active_key:
        return {
            "status": "blocked",
            "reason": "managed_key_missing",
            "rotated": False,
        }
    active_key_text = active_key.decode("ascii")
    if active_key_text != current_key:
        clear_cache()
        refreshed = get_encryption_key()
        if not refreshed or refreshed.decode("ascii") != current_key:
            return {
                "status": "blocked",
                "reason": "active_key_source_mismatch",
                "rotated": False,
            }

    previous_key = str(payload.get(_PREVIOUS_FIELD) or "")
    if previous_key:
        result = rotate_credential_encryption_material(
            db,
            old_key=previous_key,
            new_key=current_key,
            commit=True,
        )
        retire_after = _parse_datetime(payload.get(_RETIRE_AFTER_FIELD))
        retired = bool(retire_after and now >= retire_after)
        if retired and not _retire_previous_key(payload):
            raise RuntimeError("Failed to retire previous credential encryption key")
        clear_cache()
        return {
            "status": "previous_key_retired" if retired else "grace_period",
            "rotated": False,
            "updated_records": result.updated_records,
            "updated_values": result.updated_values,
            "retire_after": retire_after.isoformat() if retire_after else None,
        }

    rotated_at = _parse_datetime(payload.get(_ROTATED_AT_FIELD))
    if rotated_at is None:
        initialized = dict(payload)
        initialized[_ROTATED_AT_FIELD] = now.isoformat()
        if not _write_keyring(initialized):
            raise RuntimeError("Failed to initialize credential rotation metadata")
        return {
            "status": "initialized",
            "rotated": False,
            "next_rotation_at": (now + timedelta(days=interval_days)).isoformat(),
        }

    due_at = rotated_at + timedelta(days=interval_days)
    if now < due_at:
        return {
            "status": "not_due",
            "rotated": False,
            "next_rotation_at": due_at.isoformat(),
        }
    if not auto_apply:
        logger.warning("credential_rotation_due: automatic apply is disabled")
        return {
            "status": "due",
            "rotated": False,
            "next_rotation_at": due_at.isoformat(),
        }

    new_key = generate_encryption_key()
    staged = dict(payload)
    staged[_CURRENT_FIELD] = new_key
    staged[_PREVIOUS_FIELD] = current_key
    staged[_ROTATED_AT_FIELD] = now.isoformat()
    staged[_RETIRE_AFTER_FIELD] = (now + timedelta(days=grace_days)).isoformat()
    if not _write_keyring(staged):
        raise RuntimeError("Failed to stage credential encryption keyring")

    clear_cache()
    result = rotate_credential_encryption_material(
        db,
        old_key=current_key,
        new_key=new_key,
        commit=True,
    )
    logger.warning(
        "credential_rotation_completed: records=%d values=%d grace_days=%d",
        result.updated_records,
        result.updated_values,
        grace_days,
    )
    return {
        "status": "rotated",
        "rotated": True,
        "updated_records": result.updated_records,
        "updated_values": result.updated_values,
        "previous_key_retire_after": staged[_RETIRE_AFTER_FIELD],
    }


def run_scheduled_credential_rotation() -> dict[str, object]:
    """Single-flight scheduled entry point."""
    with db_session_adapter.advisory_lock(
        _ROTATION_LOCK_KEY,
        timeout_ms=5000,
    ) as (db, acquired):
        if not acquired:
            return {"status": "already_running", "rotated": False}
        return evaluate_scheduled_rotation(db)
