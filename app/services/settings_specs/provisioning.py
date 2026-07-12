"""Provisioning domain setting specs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType


@dataclass(frozen=True)
class ProvisioningDefaults:
    tr069_bootstrap_timeout_sec: int = 120
    tr069_bootstrap_poll_interval_sec: int = 10
    tr069_task_ready_timeout_sec: int = 45
    tr069_task_ready_poll_interval_sec: int = 5
    pppoe_push_max_attempts: int = 3
    pppoe_push_retry_delay_sec: int = 10
    stale_runtime_hours: int = 24
    olt_write_mode_enabled: bool = False
    pppoe_provisioning_method: str = "auto"
    verification_interval_sec: int = 300
    verification_staleness_minutes: int = 15
    drift_handling_mode: str = "alert_only"
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_backoff_sec: int = 30
    service_port_pool_min_index: int = 0
    service_port_pool_max_index: int = 65535


DEFAULTS = ProvisioningDefaults()


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
            default=DEFAULTS.olt_write_mode_enabled,
            label="OLT Write Mode Enabled",
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="tr069_bootstrap_timeout_sec",
            env_var="TR069_BOOTSTRAP_TIMEOUT_SEC",
            value_type=SettingValueType.integer,
            default=DEFAULTS.tr069_bootstrap_timeout_sec,
            min_value=10,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="tr069_bootstrap_poll_interval_sec",
            env_var="TR069_BOOTSTRAP_POLL_INTERVAL_SEC",
            value_type=SettingValueType.integer,
            default=DEFAULTS.tr069_bootstrap_poll_interval_sec,
            min_value=1,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="tr069_task_ready_timeout_sec",
            env_var="TR069_TASK_READY_TIMEOUT_SEC",
            value_type=SettingValueType.integer,
            default=DEFAULTS.tr069_task_ready_timeout_sec,
            min_value=5,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="tr069_task_ready_poll_interval_sec",
            env_var="TR069_TASK_READY_POLL_INTERVAL_SEC",
            value_type=SettingValueType.integer,
            default=DEFAULTS.tr069_task_ready_poll_interval_sec,
            min_value=1,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="pppoe_push_max_attempts",
            env_var="PPPOE_PUSH_MAX_ATTEMPTS",
            value_type=SettingValueType.integer,
            default=DEFAULTS.pppoe_push_max_attempts,
            min_value=1,
            max_value=20,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="pppoe_push_retry_delay_sec",
            env_var="PPPOE_PUSH_RETRY_DELAY_SEC",
            value_type=SettingValueType.integer,
            default=DEFAULTS.pppoe_push_retry_delay_sec,
            min_value=1,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="stale_runtime_hours",
            env_var="PROVISIONING_STALE_RUNTIME_HOURS",
            value_type=SettingValueType.integer,
            default=DEFAULTS.stale_runtime_hours,
            min_value=1,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="pppoe_provisioning_method",
            env_var="PPPOE_PROVISIONING_METHOD",
            value_type=SettingValueType.string,
            default=DEFAULTS.pppoe_provisioning_method,
            allowed={"auto", "omci", "tr069"},
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="verification_interval_sec",
            env_var="PROVISIONING_VERIFICATION_INTERVAL_SEC",
            value_type=SettingValueType.integer,
            default=DEFAULTS.verification_interval_sec,
            min_value=30,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="verification_staleness_minutes",
            env_var="PROVISIONING_VERIFICATION_STALENESS_MINUTES",
            value_type=SettingValueType.integer,
            default=DEFAULTS.verification_staleness_minutes,
            min_value=1,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="drift_handling_mode",
            env_var="PROVISIONING_DRIFT_HANDLING_MODE",
            value_type=SettingValueType.string,
            default=DEFAULTS.drift_handling_mode,
            allowed={"alert_only", "auto_repair"},
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="circuit_breaker_failure_threshold",
            env_var="PROVISIONING_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
            value_type=SettingValueType.integer,
            default=DEFAULTS.circuit_breaker_failure_threshold,
            min_value=1,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="circuit_breaker_backoff_sec",
            env_var="PROVISIONING_CIRCUIT_BREAKER_BACKOFF_SEC",
            value_type=SettingValueType.integer,
            default=DEFAULTS.circuit_breaker_backoff_sec,
            min_value=1,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="service_port_pool_min_index",
            env_var="SERVICE_PORT_POOL_MIN_INDEX",
            value_type=SettingValueType.integer,
            default=DEFAULTS.service_port_pool_min_index,
            min_value=0,
            max_value=65535,
        ),
        setting_spec(
            domain=SettingDomain.provisioning,
            key="service_port_pool_max_index",
            env_var="SERVICE_PORT_POOL_MAX_INDEX",
            value_type=SettingValueType.integer,
            default=DEFAULTS.service_port_pool_max_index,
            min_value=0,
            max_value=65535,
        ),
    ]
