"""Admin projection for Meta Facebook/Instagram Team Inbox configuration."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.settings_cache import SettingsCache

AUTH_MODE_OAUTH = "oauth"
AUTH_MODE_OVERRIDE = "override"
AUTH_MODE_OPTIONS = [
    {
        "id": AUTH_MODE_OAUTH,
        "label": "Meta OAuth",
    },
    {
        "id": AUTH_MODE_OVERRIDE,
        "label": "Override tokens",
    },
]

_SETTINGS: dict[str, bool] = {
    "meta_social_auth_mode": False,
    "meta_app_id": False,
    "meta_app_secret": True,
    "meta_webhook_verify_token": True,
    "meta_graph_api_version": False,
    "meta_facebook_access_token_override": True,
    "meta_instagram_access_token_override": True,
}


def _setting(db: Session, key: str) -> DomainSetting | None:
    return (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.comms, DomainSetting.key == key)
        .one_or_none()
    )


def _value(row: DomainSetting | None) -> str:
    if row is None:
        return ""
    value = (
        row.value_json if row.value_type == SettingValueType.json else row.value_text
    )
    return "" if value is None else str(value)


def _mask(value: str) -> str:
    if not value:
        return ""
    if value.startswith(("bao://", "openbao://", "vault://", "env://")):
        if len(value) <= 14:
            return "*" * len(value)
        return f"{value[:7]}...{value[-5:]}"
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"stored len={len(value)} sha={digest}"


def build_config_state(db: Session, *, base_url: str) -> dict[str, Any]:
    rows = {key: _setting(db, key) for key in _SETTINGS}
    auth_mode = _value(rows["meta_social_auth_mode"]) or AUTH_MODE_OAUTH
    if auth_mode not in {AUTH_MODE_OAUTH, AUTH_MODE_OVERRIDE}:
        auth_mode = AUTH_MODE_OAUTH
    return {
        "auth_mode_options": AUTH_MODE_OPTIONS,
        "callback_url": base_url.rstrip("/") + "/api/v1/webhooks/meta",
        "form": {
            "auth_mode": auth_mode,
            "meta_app_id": _value(rows["meta_app_id"]),
            "meta_graph_api_version": _value(rows["meta_graph_api_version"]) or "v21.0",
            "meta_app_secret": "",
            "meta_webhook_verify_token": "",
            "meta_facebook_access_token_override": "",
            "meta_instagram_access_token_override": "",
            "meta_app_secret_masked": _mask(_value(rows["meta_app_secret"])),
            "meta_webhook_verify_token_masked": _mask(
                _value(rows["meta_webhook_verify_token"])
            ),
            "meta_facebook_access_token_override_masked": _mask(
                _value(rows["meta_facebook_access_token_override"])
            ),
            "meta_instagram_access_token_override_masked": _mask(
                _value(rows["meta_instagram_access_token_override"])
            ),
        },
    }


def _upsert(db: Session, key: str, value: str, *, is_secret: bool) -> None:
    row = _setting(db, key)
    if row is None:
        row = DomainSetting(domain=SettingDomain.comms, key=key)
        db.add(row)
    row.value_type = SettingValueType.string
    row.value_text = value
    row.value_json = None
    row.is_secret = is_secret
    row.is_active = True
    row.updated_at = datetime.now(UTC)
    SettingsCache.invalidate(SettingDomain.comms.value, key)


def save_config(
    db: Session,
    *,
    auth_mode: str,
    meta_app_id: str,
    meta_app_secret: str,
    meta_webhook_verify_token: str,
    meta_graph_api_version: str,
    meta_facebook_access_token_override: str,
    meta_instagram_access_token_override: str,
) -> None:
    clean_auth_mode = auth_mode.strip().lower() or AUTH_MODE_OAUTH
    if clean_auth_mode not in {AUTH_MODE_OAUTH, AUTH_MODE_OVERRIDE}:
        raise ValueError("Unsupported Meta social auth mode")

    _upsert(
        db,
        "meta_social_auth_mode",
        clean_auth_mode,
        is_secret=False,
    )
    _upsert(db, "meta_app_id", meta_app_id.strip(), is_secret=False)
    _upsert(
        db,
        "meta_graph_api_version",
        meta_graph_api_version.strip() or "v21.0",
        is_secret=False,
    )
    for key, candidate in (
        ("meta_app_secret", meta_app_secret),
        ("meta_webhook_verify_token", meta_webhook_verify_token),
        ("meta_facebook_access_token_override", meta_facebook_access_token_override),
        ("meta_instagram_access_token_override", meta_instagram_access_token_override),
    ):
        value = candidate.strip()
        if value:
            _upsert(db, key, value, is_secret=True)
    db.commit()
