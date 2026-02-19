"""Service helpers for admin system health page."""

from __future__ import annotations

from typing import Any

from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services import system_health as system_health_service


def build_health_data(db) -> dict[str, object]:
    health = system_health_service.get_system_health()
    thresholds_raw: dict[str, Any] = {
        "disk_warn_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_warn_pct"
        ),
        "disk_crit_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_disk_crit_pct"
        ),
        "mem_warn_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_warn_pct"
        ),
        "mem_crit_pct": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_mem_crit_pct"
        ),
        "load_warn": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_warn"
        ),
        "load_crit": settings_spec.resolve_value(
            db, SettingDomain.network_monitoring, "server_health_load_crit"
        ),
    }
    thresholds: dict[str, float | None] = {}
    for key, value in thresholds_raw.items():
        try:
            thresholds[key] = float(str(value)) if value is not None else None
        except (TypeError, ValueError):
            thresholds[key] = None
    health_status = system_health_service.evaluate_health(health, thresholds)
    return {"health": health, "health_status": health_status}
