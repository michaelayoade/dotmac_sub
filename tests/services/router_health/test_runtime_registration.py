"""Controls, settings, routes and schedule for the PR2 runtime poll (all default off)."""

from __future__ import annotations

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.services import control_registry, settings_spec
from app.services.router_health.settings import RouterHealthSettings, load_settings
from app.services.settings_specs.router_health import INTEGER_SETTING_KEYS
from app.services.task_reliability import TASK_RELIABILITY_CONTRACTS


def control(key: str) -> control_registry.Control:
    return next(c for c in control_registry.all_controls() if c.key == key)


def test_new_controls_default_off_and_fail_closed() -> None:
    for key in (
        "network.router_health_runtime_poll",
        "network.router_health_optics",
    ):
        c = control(key)
        assert c.default is False and c.on_missing is False
        assert c.layer is control_registry.Layer.feature


def test_optics_control_describes_the_post() -> None:
    assert "POST" in control("network.router_health_optics").description


def test_runtime_settings_are_bounded_integers_with_design_defaults() -> None:
    expected = {
        "router_health_runtime_interval_minutes": (15, 10, 60),
        "router_health_runtime_concurrency": (6, 1, 12),
        "router_health_runtime_connect_timeout_seconds": (5, 2, 30),
        "router_health_runtime_read_timeout_seconds": (10, 2, 30),
        "router_health_runtime_router_deadline_seconds": (45, 15, 120),
        "router_health_unreachable_polls": (2, 1, 10),
        "router_health_ntp_grace_minutes": (30, 5, 1440),
        "router_health_clock_skew_seconds": (60, 5, 3600),
        "router_health_bgp_grace_minutes": (30, 5, 1440),
        "router_health_ospf_grace_minutes": (30, 5, 1440),
        "router_health_link_downs_24h_warn": (20, 1, 100000),
        "router_health_sfp_rx_low_dbm": (-22, -40, 10),
        "router_health_sfp_rx_critical_dbm": (-25, -40, 10),
        "router_health_sfp_overload_dbm": (2, -40, 10),
        "router_health_ppp_drop_pct": (50, 10, 95),
        "router_health_ppp_min_baseline": (10, 1, 100000),
    }
    for key, (default, low, high) in expected.items():
        assert key in INTEGER_SETTING_KEYS
        spec = settings_spec.get_spec(SettingDomain.network_monitoring, key)
        assert spec is not None, key
        assert (spec.default, spec.min_value, spec.max_value) == (default, low, high)


def test_settings_loader_defaults_match_the_dataclass(db_session) -> None:
    assert load_settings(db_session) == RouterHealthSettings()


def test_poll_task_is_routed_to_ingestion_and_classified() -> None:
    name = "app.tasks.router_health.poll_router_runtime"
    route = celery_app.conf.task_routes[name]
    assert route == {"queue": "ingestion"}
    assert name in TASK_RELIABILITY_CONTRACTS
    import app.tasks  # noqa: F401  registers the task

    assert name in celery_app.tasks
