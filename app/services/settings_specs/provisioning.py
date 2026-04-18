"""Provisioning domain setting specs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType


def build_provisioning_specs(setting_spec: Callable[..., Any]) -> list[Any]:
    """Build provisioning setting specs without importing the main registry."""
    return [
        setting_spec(
            domain=SettingDomain.provisioning,
            key="nas_backup_retention_interval_seconds",
            env_var="NAS_BACKUP_RETENTION_INTERVAL",
            value_type=SettingValueType.integer,
            default=86400,
            min_value=3600,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="oauth_token_refresh_interval_seconds",
            env_var="OAUTH_TOKEN_REFRESH_INTERVAL",
            value_type=SettingValueType.integer,
            default=86400,
            min_value=3600,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="olt_write_mode_enabled",
            env_var="OLT_WRITE_MODE_ENABLED",
            value_type=SettingValueType.boolean,
            default=False,
            label="OLT Write Mode Enabled",
        ),
    ]
