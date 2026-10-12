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
