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
    runtime_interval_minutes: int = 15
    runtime_concurrency: int = 6
    runtime_connect_timeout_seconds: int = 5
    runtime_read_timeout_seconds: int = 10
    runtime_router_deadline_seconds: int = 45
    unreachable_polls: int = 2
    ntp_grace_minutes: int = 30
    clock_skew_seconds: int = 60
    clock_skew_high_seconds: int = 3600
    bgp_grace_minutes: int = 30
    ospf_grace_minutes: int = 30
    link_downs_24h_warn: int = 20
    link_downs_24h_high: int = 200
    sfp_rx_low_dbm: int = -22
    sfp_rx_critical_dbm: int = -25
    sfp_overload_dbm: int = 2
    ppp_drop_pct: int = 50
    ppp_min_baseline: int = 10


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
            "router_health_runtime_interval_minutes",
            "router_health_runtime_concurrency",
            "router_health_runtime_connect_timeout_seconds",
            "router_health_runtime_read_timeout_seconds",
            "router_health_runtime_router_deadline_seconds",
            "router_health_unreachable_polls",
            "router_health_ntp_grace_minutes",
            "router_health_clock_skew_seconds",
            "router_health_clock_skew_high_seconds",
            "router_health_bgp_grace_minutes",
            "router_health_ospf_grace_minutes",
            "router_health_link_downs_24h_warn",
            "router_health_link_downs_24h_high",
            "router_health_sfp_rx_low_dbm",
            "router_health_sfp_rx_critical_dbm",
            "router_health_sfp_overload_dbm",
            "router_health_ppp_drop_pct",
            "router_health_ppp_min_baseline",
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
        runtime_interval_minutes=number(
            "runtime_interval_minutes", defaults.runtime_interval_minutes
        ),
        runtime_concurrency=number("runtime_concurrency", defaults.runtime_concurrency),
        runtime_connect_timeout_seconds=number(
            "runtime_connect_timeout_seconds", defaults.runtime_connect_timeout_seconds
        ),
        runtime_read_timeout_seconds=number(
            "runtime_read_timeout_seconds", defaults.runtime_read_timeout_seconds
        ),
        runtime_router_deadline_seconds=number(
            "runtime_router_deadline_seconds", defaults.runtime_router_deadline_seconds
        ),
        unreachable_polls=number("unreachable_polls", defaults.unreachable_polls),
        ntp_grace_minutes=number("ntp_grace_minutes", defaults.ntp_grace_minutes),
        clock_skew_seconds=number("clock_skew_seconds", defaults.clock_skew_seconds),
        clock_skew_high_seconds=number(
            "clock_skew_high_seconds", defaults.clock_skew_high_seconds
        ),
        bgp_grace_minutes=number("bgp_grace_minutes", defaults.bgp_grace_minutes),
        ospf_grace_minutes=number("ospf_grace_minutes", defaults.ospf_grace_minutes),
        link_downs_24h_warn=number("link_downs_24h_warn", defaults.link_downs_24h_warn),
        link_downs_24h_high=number("link_downs_24h_high", defaults.link_downs_24h_high),
        sfp_rx_low_dbm=number("sfp_rx_low_dbm", defaults.sfp_rx_low_dbm),
        sfp_rx_critical_dbm=number("sfp_rx_critical_dbm", defaults.sfp_rx_critical_dbm),
        sfp_overload_dbm=number("sfp_overload_dbm", defaults.sfp_overload_dbm),
        ppp_drop_pct=number("ppp_drop_pct", defaults.ppp_drop_pct),
        ppp_min_baseline=number("ppp_min_baseline", defaults.ppp_min_baseline),
    )
