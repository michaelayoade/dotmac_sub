"""Centralized provisioning settings with configurable defaults.

This module provides access to provisioning-related settings via DomainSettings,
with fallback defaults for cases where settings haven't been configured.

All timeouts, retry counts, and intervals that affect OLT/ONT provisioning
behavior should be defined here to allow operator tuning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.settings_cache import SettingsCache

logger = logging.getLogger(__name__)


# Default values — used when settings are not configured in DB
@dataclass(frozen=True)
class ProvisioningDefaults:
    """Default values for provisioning settings.

    These are used when no DomainSetting is configured. Values can be
    overridden by creating settings in the 'provisioning' domain.
    """

    # TR-069 bootstrap polling
    tr069_bootstrap_timeout_sec: int = 120
    tr069_bootstrap_poll_interval_sec: int = 10
    tr069_task_ready_timeout_sec: int = 45
    tr069_task_ready_poll_interval_sec: int = 5

    # PPPoE push retries
    pppoe_push_max_attempts: int = 3
    pppoe_push_retry_delay_sec: int = 10

    # OLT autofind
    autofind_candidate_freshness_sec: int = 300
    force_reauthorize_autofind_attempts: int = 3
    force_reauthorize_retry_delay_sec: float = 2.0

    # Enforcement
    stale_runtime_hours: int = 24


DEFAULTS = ProvisioningDefaults()

# Setting keys in the 'provisioning' domain
SETTING_KEYS = {
    "tr069_bootstrap_timeout_sec": DEFAULTS.tr069_bootstrap_timeout_sec,
    "tr069_bootstrap_poll_interval_sec": DEFAULTS.tr069_bootstrap_poll_interval_sec,
    "tr069_task_ready_timeout_sec": DEFAULTS.tr069_task_ready_timeout_sec,
    "tr069_task_ready_poll_interval_sec": DEFAULTS.tr069_task_ready_poll_interval_sec,
    "pppoe_push_max_attempts": DEFAULTS.pppoe_push_max_attempts,
    "pppoe_push_retry_delay_sec": DEFAULTS.pppoe_push_retry_delay_sec,
    "autofind_candidate_freshness_sec": DEFAULTS.autofind_candidate_freshness_sec,
    "force_reauthorize_autofind_attempts": DEFAULTS.force_reauthorize_autofind_attempts,
    "force_reauthorize_retry_delay_sec": DEFAULTS.force_reauthorize_retry_delay_sec,
    "stale_runtime_hours": DEFAULTS.stale_runtime_hours,
}


def _get_setting_from_cache(key: str) -> Any | None:
    """Try to get a setting from Redis cache first."""
    return SettingsCache.get(SettingDomain.provisioning.value, key)


def _get_setting_from_db(db: Session, key: str) -> Any | None:
    """Get a setting from the database and cache it."""
    from app.models.domain_settings import DomainSetting

    setting = (
        db.query(DomainSetting)
        .filter(
            DomainSetting.domain == SettingDomain.provisioning,
            DomainSetting.key == key,
            DomainSetting.is_active.is_(True),
        )
        .first()
    )
    if not setting:
        return None

    # Extract value based on type
    value: Any
    if setting.value_json is not None:
        value = setting.value_json
    else:
        value = setting.value_text

    # Cache for future lookups
    SettingsCache.set(SettingDomain.provisioning.value, key, value)
    return value


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

    # Try cache first
    cached = _get_setting_from_cache(key)
    if cached is not None:
        return cached

    # Try DB if session provided
    if db is not None:
        db_value = _get_setting_from_db(db, key)
        if db_value is not None:
            return db_value

    return default


def get_int_setting(db: Session | None, key: str, default: int | None = None) -> int:
    """Get an integer provisioning setting."""
    value = get_setting(db, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default if default is not None else int(SETTING_KEYS.get(key, 0))


def get_float_setting(db: Session | None, key: str, default: float | None = None) -> float:
    """Get a float provisioning setting."""
    value = get_setting(db, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default if default is not None else float(SETTING_KEYS.get(key, 0.0))


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


def get_autofind_freshness_sec(db: Session | None = None) -> int:
    """Get autofind candidate freshness threshold in seconds."""
    return get_int_setting(db, "autofind_candidate_freshness_sec")


def get_force_reauthorize_attempts(db: Session | None = None) -> int:
    """Get force reauthorize retry attempts."""
    return get_int_setting(db, "force_reauthorize_autofind_attempts")


def get_force_reauthorize_retry_delay(db: Session | None = None) -> float:
    """Get force reauthorize retry delay in seconds."""
    return get_float_setting(db, "force_reauthorize_retry_delay_sec")


def get_stale_runtime_hours(db: Session | None = None) -> int:
    """Get stale runtime data threshold in hours."""
    return get_int_setting(db, "stale_runtime_hours")
