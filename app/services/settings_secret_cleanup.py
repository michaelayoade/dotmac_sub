"""Helpers for remediating plaintext secret settings into OpenBao refs."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting
from app.services.credential_crypto import is_encrypted
from app.services.secrets import (
    is_openbao_available,
    is_openbao_ref,
    read_secret_fields,
    write_secret,
)
from app.services.settings_cache import SettingsCache

SECRET_SETTINGS_PATH_PREFIX = "settings"


@dataclass(frozen=True)
class SecretCleanupResult:
    migrated: int
    skipped: int
    errors: list[str]
    migrated_keys: list[str]
    skipped_keys: list[str]


def openbao_secret_path(setting: DomainSetting) -> str:
    return f"{SECRET_SETTINGS_PATH_PREFIX}/{setting.domain.value}"


def openbao_secret_ref(setting: DomainSetting) -> str:
    return f"bao://secret/{SECRET_SETTINGS_PATH_PREFIX}/{setting.domain.value}#{setting.key}"


def is_plaintext_secret_setting(setting: DomainSetting) -> bool:
    if not setting.is_active or not setting.is_secret:
        return False
    if setting.value_text is None:
        return False
    value = str(setting.value_text).strip()
    if not value:
        return False
    if is_openbao_ref(value):
        return False
    if is_encrypted(value):
        return False
    return True


def find_plaintext_secret_settings(
    db: Session,
    *,
    domain: str | None = None,
    key: str | None = None,
) -> list[DomainSetting]:
    query = (
        db.query(DomainSetting)
        .filter(DomainSetting.is_active.is_(True))
        .filter(DomainSetting.is_secret.is_(True))
        .filter(DomainSetting.value_text.is_not(None))
    )
    if domain:
        query = query.filter(DomainSetting.domain == domain)
    if key:
        query = query.filter(DomainSetting.key == key)
    rows = query.order_by(DomainSetting.domain, DomainSetting.key).all()
    return [row for row in rows if is_plaintext_secret_setting(row)]


def migrate_plaintext_secret_settings(
    db: Session,
    *,
    dry_run: bool = True,
    domain: str | None = None,
    key: str | None = None,
) -> SecretCleanupResult:
    candidates = find_plaintext_secret_settings(db, domain=domain, key=key)
    migrated_keys: list[str] = []
    skipped_keys: list[str] = []
    errors: list[str] = []

    if not candidates:
        return SecretCleanupResult(
            migrated=0,
            skipped=0,
            errors=[],
            migrated_keys=[],
            skipped_keys=[],
        )

    if not dry_run and not is_openbao_available():
        return SecretCleanupResult(
            migrated=0,
            skipped=0,
            errors=["OpenBao is not configured or reachable."],
            migrated_keys=[],
            skipped_keys=[],
        )

    migrated = 0
    skipped = 0

    for setting in candidates:
        key_name = f"{setting.domain.value}.{setting.key}"
        if dry_run:
            migrated_keys.append(key_name)
            migrated += 1
            continue

        path = openbao_secret_path(setting)
        existing = read_secret_fields(path, masked=False)
        payload = dict(existing)
        payload[setting.key] = str(setting.value_text or "")

        if not write_secret(path, payload):
            errors.append(f"{key_name}: failed to write OpenBao secret")
            skipped += 1
            skipped_keys.append(key_name)
            continue

        setting.value_text = openbao_secret_ref(setting)
        SettingsCache.invalidate(setting.domain.value, setting.key)
        migrated += 1
        migrated_keys.append(key_name)

    if not dry_run and migrated:
        db.commit()

    return SecretCleanupResult(
        migrated=migrated,
        skipped=skipped,
        errors=errors,
        migrated_keys=migrated_keys,
        skipped_keys=skipped_keys,
    )
