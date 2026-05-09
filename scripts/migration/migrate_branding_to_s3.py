"""Migrate local branding assets referenced in settings to S3-backed branding URLs."""

from __future__ import annotations

from pathlib import Path

from app.db import SessionLocal
from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import branding_storage as branding_storage_service
from app.services import settings_spec
from app.services.object_storage import ensure_storage_bucket
from app.services.web_system_settings_views import (
    FAVICON_SETTING_KEY,
    SIDEBAR_LOGO_DARK_SETTING_KEY,
    SIDEBAR_LOGO_SETTING_KEY,
)

KEYS = [SIDEBAR_LOGO_SETTING_KEY, SIDEBAR_LOGO_DARK_SETTING_KEY, FAVICON_SETTING_KEY]


def _resolve_local_path(url: str) -> Path:
    relative = url.lstrip("/")
    return (Path.cwd() / relative).resolve()


def main() -> None:
    ensure_storage_bucket()
    db = SessionLocal()
    migrated = 0
    skipped = 0
    missing = 0
    try:
        service = settings_spec.DOMAIN_SETTINGS_SERVICE[SettingDomain.comms]
        for key in KEYS:
            setting = service.get_by_key(db, key)
            if not setting or not setting.value_text:
                skipped += 1
                continue
            current = setting.value_text.strip()
            if branding_storage_service.is_managed_branding_url(current):
                skipped += 1
                continue
            if not current.startswith("/static/branding/"):
                skipped += 1
                continue

            local_path = _resolve_local_path(current)
            if not local_path.exists() or not local_path.is_file():
                missing += 1
                continue

            payload = local_path.read_bytes()
            uploaded = branding_storage_service.upload_branding_asset(
                db=db,
                setting_key=key,
                file_data=payload,
                content_type=None,
                filename=local_path.name,
                uploaded_by=None,
            )
            new_url = branding_storage_service.branding_url_for_file(uploaded.id)
            service.upsert_by_key(
                db,
                key,
                DomainSettingUpdate(
                    value_type=SettingValueType.string,
                    value_text=new_url,
                    value_json=None,
                    is_secret=False,
                    is_active=True,
                ),
            )
            migrated += 1

        print(
            f"Migrated {migrated} branding settings, skipped {skipped}, missing_local_files {missing}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
