"""Typed reader for router health settings (see ``settings_specs.router_health``)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.router_health.contracts import ALERT_MODE_ALERT, ALERT_MODE_SHADOW


@dataclass(frozen=True)
class RouterHealthSettings:
    alert_mode: str = ALERT_MODE_SHADOW
    posture_interval_hours: int = 6
    snapshot_max_age_hours: int = 36
    snapshot_high_age_hours: int = 168
    service_path_interval_minutes: int = 60
    coa_window_hours: int = 72
    coa_timeout_min_count: int = 3
    coa_timeout_ratio_pct: int = 20
    enforcement_failure_min_count: int = 3
    coa_canary_interval_hours: int = 24
    coa_canary_min_spacing_hours: int = 20
    coa_canary_max_targets: int = 50
    coa_canary_spacing_ms: int = 2000
    coa_canary_reprobe_seconds: int = 300
    max_findings_per_run: int = 300


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def load_settings(db: Session) -> RouterHealthSettings:
    """Resolve every router-health setting; invalid values degrade to defaults."""
    defaults = RouterHealthSettings()
    resolved = settings_spec.resolve_values_atomic(
        db,
        SettingDomain.network_monitoring,
        [
            "router_health_alert_mode",
            "router_health_posture_interval_hours",
            "router_health_snapshot_max_age_hours",
            "router_health_snapshot_high_age_hours",
            "router_health_service_path_interval_minutes",
            "router_health_coa_window_hours",
            "router_health_coa_timeout_min_count",
            "router_health_coa_timeout_ratio_pct",
            "router_health_enforcement_failure_min_count",
            "router_health_coa_canary_interval_hours",
            "router_health_coa_canary_min_spacing_hours",
            "router_health_coa_canary_max_targets",
            "router_health_coa_canary_spacing_ms",
            "router_health_coa_canary_reprobe_seconds",
            "router_health_max_findings_per_run",
        ],
    )

    def number(key: str, default: int) -> int:
        return _as_int(resolved.get(f"router_health_{key}"), default)

    mode = str(resolved.get("router_health_alert_mode") or ALERT_MODE_SHADOW)
    return RouterHealthSettings(
        # Anything other than the explicit "alert" stays shadow (fail closed).
        alert_mode=ALERT_MODE_ALERT if mode == ALERT_MODE_ALERT else ALERT_MODE_SHADOW,
        posture_interval_hours=number(
            "posture_interval_hours", defaults.posture_interval_hours
        ),
        snapshot_max_age_hours=number(
            "snapshot_max_age_hours", defaults.snapshot_max_age_hours
        ),
        snapshot_high_age_hours=number(
            "snapshot_high_age_hours", defaults.snapshot_high_age_hours
        ),
        service_path_interval_minutes=number(
            "service_path_interval_minutes", defaults.service_path_interval_minutes
        ),
        coa_window_hours=number("coa_window_hours", defaults.coa_window_hours),
        coa_timeout_min_count=number(
            "coa_timeout_min_count", defaults.coa_timeout_min_count
        ),
        coa_timeout_ratio_pct=number(
            "coa_timeout_ratio_pct", defaults.coa_timeout_ratio_pct
        ),
        enforcement_failure_min_count=number(
            "enforcement_failure_min_count", defaults.enforcement_failure_min_count
        ),
        coa_canary_interval_hours=number(
            "coa_canary_interval_hours", defaults.coa_canary_interval_hours
        ),
        coa_canary_min_spacing_hours=number(
            "coa_canary_min_spacing_hours", defaults.coa_canary_min_spacing_hours
        ),
        coa_canary_max_targets=number(
            "coa_canary_max_targets", defaults.coa_canary_max_targets
        ),
        coa_canary_spacing_ms=number(
            "coa_canary_spacing_ms", defaults.coa_canary_spacing_ms
        ),
        coa_canary_reprobe_seconds=number(
            "coa_canary_reprobe_seconds", defaults.coa_canary_reprobe_seconds
        ),
        max_findings_per_run=number(
            "max_findings_per_run", defaults.max_findings_per_run
        ),
    )
