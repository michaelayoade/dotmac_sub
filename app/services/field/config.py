"""Public mobile field-app configuration.

The force-upgrade gate must work before login, so the payload is deliberately
small and contains no secrets.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_values_atomic

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
        values = resolve_values_atomic(db, SettingDomain.field, list(_KEYS))
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
