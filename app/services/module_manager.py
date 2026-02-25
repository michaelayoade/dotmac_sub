"""Feature module toggle management."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.settings_cache import SettingsCache

_CACHE_DOMAIN = "modules"
_CACHE_KEY = "states"

MODULE_KEY_MAP: dict[str, str] = {
    "catalog": "module_catalog_enabled",
    "customer": "module_customer_enabled",
    "network": "module_network_enabled",
    "billing": "module_billing_enabled",
    "notifications": "module_notifications_enabled",
    "integrations": "module_integrations_enabled",
    "inventory": "module_inventory_enabled",
    "helpdesk": "module_helpdesk_enabled",
    "scheduling": "module_scheduling_enabled",
    "voice": "module_voice_enabled",
    "crm": "module_crm_enabled",
}


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def invalidate_module_cache() -> None:
    """Invalidate cached module state map."""
    SettingsCache.invalidate(_CACHE_DOMAIN, _CACHE_KEY)


def load_module_states(db: Session, *, force_refresh: bool = False) -> dict[str, bool]:
    """Load all module flags, using Redis cache for fast route checks."""
    if not force_refresh:
        cached = SettingsCache.get(_CACHE_DOMAIN, _CACHE_KEY)
        if isinstance(cached, dict):
            return {str(k): _coerce_bool(v) for k, v in cached.items()}

    states: dict[str, bool] = {}
    for module_name, setting_key in MODULE_KEY_MAP.items():
        raw = settings_spec.resolve_value(db, SettingDomain.workflow, setting_key)
        states[module_name] = _coerce_bool(raw, default=True)

    SettingsCache.set(_CACHE_DOMAIN, _CACHE_KEY, states)
    return states


def is_module_enabled(db: Session, module_name: str) -> bool:
    """Return whether a module is enabled."""
    states = load_module_states(db)
    return states.get(module_name, True)


def require_module_enabled(module_name: str) -> Callable[..., None]:
    """Dependency factory that returns 404 when a module is disabled."""

    def _dependency(db: Session = Depends(get_db)) -> None:
        if not is_module_enabled(db, module_name):
            raise HTTPException(status_code=404, detail="Not found")

    return _dependency
