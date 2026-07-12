"""Centralized provisioning settings with configurable defaults.

This module provides access to provisioning-related settings via DomainSettings,
with fallback defaults for cases where settings haven't been configured.

All timeouts, retry counts, and intervals that affect OLT/ONT provisioning
behavior should be defined here to allow operator tuning.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.settings_specs.provisioning import (  # noqa: F401 - compatibility re-export
    DEFAULTS,
    ProvisioningDefaults,
)

logger = logging.getLogger(__name__)


# Setting keys in the 'provisioning' domain
SETTING_KEYS = {field.name: getattr(DEFAULTS, field.name) for field in fields(DEFAULTS)}


def get_setting(db: Session | None, key: str, default: Any = None) -> Any:
    """Get a provisioning setting with fallback to default.

    Args:
        db: Database session. If None, only cache lookup is attempted.
        key: Setting key name.
        default: Default value if not found. If None, uses SETTING_KEYS default.

    Returns:
        The setting value, or the default if not configured.
    """
    if default is None:
        default = SETTING_KEYS.get(key)

    value = settings_spec.resolve_value(db, SettingDomain.provisioning, key)
    return default if value is None else value


def get_int_setting(db: Session | None, key: str, default: int | None = None) -> int:
    """Get an integer provisioning setting."""
    value = get_setting(db, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        fallback = SETTING_KEYS.get(key, 0)
        return default if default is not None else int(str(fallback))


def get_float_setting(
    db: Session | None, key: str, default: float | None = None
) -> float:
    """Get a float provisioning setting."""
    value = get_setting(db, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        fallback = SETTING_KEYS.get(key, 0.0)
        return default if default is not None else float(str(fallback))


def get_bool_setting(db: Session | None, key: str, default: bool | None = None) -> bool:
    """Get a boolean provisioning setting."""
    value = get_setting(db, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# Convenience functions for specific settings
def get_tr069_bootstrap_timeout(db: Session | None = None) -> int:
    """Get TR-069 bootstrap polling timeout in seconds."""
    return get_int_setting(db, "tr069_bootstrap_timeout_sec")


def get_tr069_bootstrap_poll_interval(db: Session | None = None) -> int:
    """Get TR-069 bootstrap poll interval in seconds."""
    return get_int_setting(db, "tr069_bootstrap_poll_interval_sec")


def get_pppoe_push_max_attempts(db: Session | None = None) -> int:
    """Get maximum PPPoE push retry attempts."""
    return get_int_setting(db, "pppoe_push_max_attempts")


def get_pppoe_push_retry_delay(db: Session | None = None) -> int:
    """Get PPPoE push retry delay in seconds."""
    return get_int_setting(db, "pppoe_push_retry_delay_sec")


def get_stale_runtime_hours(db: Session | None = None) -> int:
    """Get stale runtime data threshold in hours."""
    return get_int_setting(db, "stale_runtime_hours")


def get_olt_write_mode_enabled(db: Session | None = None) -> bool:
    """Return whether provisioning may execute OLT write commands."""
    return get_bool_setting(db, "olt_write_mode_enabled")


def get_pppoe_provisioning_method(db: Session | None = None) -> str:
    """Get PPPoE provisioning method preference.

    Returns one of:
    - "auto": Try OMCI first, fall back to TR-069 on failure (default)
    - "omci": Only use OLT OMCI commands
    - "tr069": Only use TR-069/GenieACS, skip OMCI entirely
    """
    value = get_setting(
        db, "pppoe_provisioning_method", DEFAULTS.pppoe_provisioning_method
    )
    normalized = str(value).strip().lower()
    if normalized in {"omci", "tr069"}:
        return normalized
    return "auto"


# Phase 2: Async verification settings
def get_verification_interval(db: Session | None = None) -> int:
    """Get verification interval in seconds (default 300 = 5 minutes)."""
    return get_int_setting(db, "verification_interval_sec")


def get_verification_staleness_minutes(db: Session | None = None) -> int:
    """Get verification staleness threshold in minutes."""
    return get_int_setting(db, "verification_staleness_minutes")


def get_drift_handling_mode(db: Session | None = None) -> str:
    """Get drift handling mode: 'alert_only' or 'auto_repair'."""
    value = get_setting(db, "drift_handling_mode", DEFAULTS.drift_handling_mode)
    normalized = str(value).strip().lower()
    if normalized in {"auto_repair"}:
        return normalized
    return "alert_only"


# Phase 4: Circuit breaker settings
def get_circuit_breaker_threshold(db: Session | None = None) -> int:
    """Get number of failures before circuit opens."""
    return get_int_setting(db, "circuit_breaker_failure_threshold")


def get_circuit_breaker_backoff(db: Session | None = None) -> int:
    """Get circuit breaker backoff period in seconds."""
    return get_int_setting(db, "circuit_breaker_backoff_sec")


# Phase 1: Service-port allocator settings
def get_service_port_pool_range(db: Session | None = None) -> tuple[int, int]:
    """Get service-port pool index range (min, max)."""
    min_idx = get_int_setting(db, "service_port_pool_min_index")
    max_idx = get_int_setting(db, "service_port_pool_max_index")
    return (min_idx, max_idx)
