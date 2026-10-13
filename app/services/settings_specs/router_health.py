"""Router health reconciler setting specs (PR1).

All thresholds are bounded integers in ``network_monitoring``; the alert mode and
the policy are ``owner_command_only`` so every change leaves an audit event.
Readers live in ``app.services.router_health.settings``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType

#: (key, label, default, min, max)
_INTEGER_SETTINGS: tuple[tuple[str, str, int, int, int], ...] = (
    (
        "router_health_posture_interval_hours",
        "Router health: posture interval (hours)",
        6,
        1,
        24,
    ),
    (
        "router_health_snapshot_max_age_hours",
        "Router health: snapshot stale after (hours)",
        36,
        6,
        168,
    ),
    (
        "router_health_snapshot_high_age_hours",
        "Router health: snapshot high priority after (hours)",
        168,
        24,
        720,
    ),
    (
        "router_health_service_path_interval_minutes",
        "Router health: service-path interval (minutes)",
        60,
        15,
        360,
    ),
    (
        "router_health_coa_window_hours",
        "Router health: CoA evaluation window (hours)",
        72,
        6,
        336,
    ),
    (
        "router_health_coa_timeout_min_count",
        "Router health: CoA timeouts before alerting",
        3,
        1,
        100,
    ),
    (
        "router_health_coa_timeout_ratio_pct",
        "Router health: CoA timeout share before alerting (%)",
        20,
        1,
        100,
    ),
    (
        "router_health_enforcement_failure_min_count",
        "Router health: enforcement failures before alerting",
        3,
        1,
        100,
    ),
    (
        "router_health_coa_canary_interval_hours",
        "Router health: CoA canary interval (hours)",
        24,
        12,
        168,
    ),
    (
        "router_health_coa_canary_min_spacing_hours",
        "Router health: minimum hours between canaries per NAS",
        20,
        1,
        168,
    ),
    (
        "router_health_coa_canary_max_targets",
        "Router health: maximum canary targets per run",
        50,
        1,
        200,
    ),
    (
        "router_health_coa_canary_spacing_ms",
        "Router health: delay between canary packets (ms)",
        2000,
        500,
        60000,
    ),
    (
        "router_health_coa_canary_reprobe_seconds",
        "Router health: canary re-probe delay (seconds)",
        300,
        60,
        3600,
    ),
    (
        "router_health_max_findings_per_run",
        "Router health: maximum findings per run",
        300,
        50,
        2000,
    ),
    (
        "router_health_runtime_interval_minutes",
        "Router health: runtime poll interval (minutes)",
        15,
        10,
        60,
    ),
    (
        "router_health_runtime_concurrency",
        "Router health: concurrent router polls",
        6,
        1,
        12,
    ),
    (
        "router_health_runtime_connect_timeout_seconds",
        "Router health: runtime poll connect timeout (seconds)",
        5,
        2,
        30,
    ),
    (
        "router_health_runtime_read_timeout_seconds",
        "Router health: runtime poll read timeout (seconds)",
        10,
        2,
        30,
    ),
    (
        "router_health_runtime_router_deadline_seconds",
        "Router health: total time allowed per router poll (seconds)",
        45,
        15,
        120,
    ),
    (
        "router_health_unreachable_polls",
        "Router health: consecutive failed polls before router-unreachable",
        2,
        1,
        10,
    ),
    (
        "router_health_ntp_grace_minutes",
        "Router health: NTP unsynchronised grace (minutes)",
        30,
        5,
        1440,
    ),
    (
        "router_health_clock_skew_seconds",
        "Router health: clock skew warning threshold (seconds)",
        60,
        5,
        3600,
    ),
    (
        "router_health_clock_skew_high_seconds",
        "Router health: clock skew high-priority threshold (seconds)",
        3600,
        60,
        604800,
    ),
    (
        "router_health_bgp_grace_minutes",
        "Router health: BGP session down grace (minutes)",
        30,
        5,
        1440,
    ),
    (
        "router_health_ospf_grace_minutes",
        "Router health: OSPF neighbour lost grace (minutes)",
        30,
        5,
        1440,
    ),
    (
        "router_health_link_downs_24h_warn",
        "Router health: interface link-downs per 24 h (warning)",
        20,
        1,
        100000,
    ),
    (
        "router_health_link_downs_24h_high",
        "Router health: interface link-downs per 24 h (high priority)",
        200,
        1,
        1000000,
    ),
    (
        "router_health_sfp_rx_low_dbm",
        "Router health: SFP receive power low (dBm, optics control only)",
        -22,
        -40,
        10,
    ),
    (
        "router_health_sfp_rx_critical_dbm",
        "Router health: SFP receive power critically low (dBm, optics control only)",
        -25,
        -40,
        10,
    ),
    (
        "router_health_sfp_overload_dbm",
        "Router health: SFP receive power overload (dBm, optics control only)",
        2,
        -40,
        10,
    ),
    (
        "router_health_ppp_drop_pct",
        "Router health: PPPoE active-session drop before alerting (%)",
        50,
        10,
        95,
    ),
    (
        "router_health_ppp_min_baseline",
        "Router health: minimum PPPoE baseline for drop detection",
        10,
        1,
        100000,
    ),
)

INTEGER_SETTING_KEYS: tuple[str, ...] = tuple(row[0] for row in _INTEGER_SETTINGS)


def build_router_health_specs(setting_spec: Callable[..., Any]) -> list[Any]:
    """Build router-health setting specs without importing the main registry."""
    specs: list[Any] = [
        setting_spec(
            domain=SettingDomain.network_monitoring,
            key=key,
            env_var=None,
            value_type=SettingValueType.integer,
            default=default,
            label=label,
            min_value=minimum,
            max_value=maximum,
        )
        for key, label, default, minimum, maximum in _INTEGER_SETTINGS
    ]
    specs.append(
        setting_spec(
            domain=SettingDomain.network_monitoring,
            key="router_health_alert_mode",
            env_var=None,
            value_type=SettingValueType.string,
            default="shadow",
            label="Router health: alert mode (shadow or alert)",
            allowed={"shadow", "alert"},
            inherits=False,
            owner_command_only=True,
        )
    )
    specs.append(
        setting_spec(
            domain=SettingDomain.network,
            key="router_health_policy",
            env_var=None,
            value_type=SettingValueType.json,
            default={"version": 1},
            label="Router health: policy (roles, overrides, dated exceptions)",
            inherits=False,
            owner_command_only=True,
        )
    )
    return specs
