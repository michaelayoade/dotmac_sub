"""Sub-local outbound integration settings.

The back-office provider selects an anti-corruption adapter. Provider-specific
credentials and delivery controls remain scoped to that adapter; no setting
turns the external system into a Sub domain owner.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType


def build_integration_specs(setting_spec: Callable[..., Any]) -> list[Any]:
    """Build integration setting specs without importing the main registry."""
    return [
        setting_spec(
            domain=SettingDomain.integration,
            key="backoffice_provider",
            env_var="BACKOFFICE_PROVIDER",
            value_type=SettingValueType.string,
            default="dotmac_erp",
            label="Back-office Provider Adapter",
        ),
        setting_spec(
            domain=SettingDomain.integration,
            key="dotmac_erp_sync_enabled",
            env_var="DOTMAC_ERP_SYNC_ENABLED",
            value_type=SettingValueType.boolean,
            default=False,
            label="DotMac ERP Sync Enabled",
        ),
        setting_spec(
            domain=SettingDomain.integration,
            key="dotmac_erp_domain_sync_enabled",
            env_var="DOTMAC_ERP_DOMAIN_SYNC_ENABLED",
            value_type=SettingValueType.boolean,
            default=False,
            label="DotMac ERP Operational Context Sync Enabled",
        ),
        setting_spec(
            domain=SettingDomain.integration,
            key="dotmac_erp_base_url",
            env_var="DOTMAC_ERP_BASE_URL",
            value_type=SettingValueType.string,
            default="https://erp.dotmac.io",
            label="DotMac ERP Base URL",
        ),
        setting_spec(
            domain=SettingDomain.integration,
            key="dotmac_erp_token",
            env_var="DOTMAC_ERP_TOKEN",
            value_type=SettingValueType.string,
            default=None,
            is_secret=True,
            label="DotMac ERP API Key",
        ),
        setting_spec(
            domain=SettingDomain.integration,
            key="dotmac_erp_timeout_seconds",
            env_var="DOTMAC_ERP_TIMEOUT_SECONDS",
            value_type=SettingValueType.integer,
            default=30,
            min_value=1,
            max_value=300,
            label="DotMac ERP Request Timeout (seconds)",
        ),
        setting_spec(
            domain=SettingDomain.integration,
            key="dotmac_erp_max_retries",
            env_var="DOTMAC_ERP_MAX_RETRIES",
            value_type=SettingValueType.integer,
            default=3,
            min_value=0,
            max_value=10,
            label="DotMac ERP Max Retries",
        ),
    ]
