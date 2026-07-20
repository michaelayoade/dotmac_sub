"""Public mobile field-app configuration.

The force-upgrade gate must work before login, so the payload is deliberately
small and contains no secrets.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain

DEFAULT_MIN_APP_VERSION = "1.0.0"
DEFAULT_LATEST_APP_VERSION = "1.0.0"
DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    "vendor_module": True,
    "offline_sync": True,
    "location_sharing": False,
}

_KEYS = (
    "mobile_min_app_version",
    "mobile_latest_app_version",
    "mobile_feature_flags",
)


class FieldConfigService:
    @staticmethod
    def get(db: Session) -> dict:
        rows = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == SettingDomain.field)
            .filter(DomainSetting.key.in_(_KEYS))
            .filter(DomainSetting.is_active.is_(True))
            .all()
        )
        values = {
            row.key: row.value_json if row.value_json is not None else row.value_text
            for row in rows
        }
        flags = values.get("mobile_feature_flags")
        if not isinstance(flags, dict):
            flags = {}
        return {
            "min_app_version": values.get("mobile_min_app_version")
            or DEFAULT_MIN_APP_VERSION,
            "latest_app_version": values.get("mobile_latest_app_version")
            or DEFAULT_LATEST_APP_VERSION,
            "feature_flags": {**DEFAULT_FEATURE_FLAGS, **flags},
        }


field_config = FieldConfigService()
