"""The monitoring overview keeps expensive panels off the initial response."""

import re
from datetime import UTC, datetime
from pathlib import Path

from starlette.requests import Request

from app.models.network_monitoring import (
    Alert,
    AlertSeverity,
    AlertStatus,
    MetricType,
)
from app.schemas.network_monitoring import AlertRuleCreate
from app.services import network_monitoring as monitoring_service
from app.services import web_network_monitoring as web_monitoring
from app.web.admin import network as network_routes
from app.web.admin import network_core_devices as core_device_routes
from app.web.admin import network_monitoring as routes


def test_glance_counts_all_open_alarms_without_metrics(
    db_session, network_device, monkeypatch
):
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="CPU Alert",
            metric_type=MetricType.cpu,
            threshold=80.0,
            severity=AlertSeverity.critical,
            device_id=network_device.id,
        ),
    )
    db_session.add_all(
        [
            Alert(
                rule_id=rule.id,
                device_id=network_device.id,
                metric_type=MetricType.cpu,
                measured_value=95.0,
                status=AlertStatus.open,
                severity=AlertSeverity.critical,
            )
            for _ in range(12)
        ]
    )
    db_session.add(
        Alert(
            rule_id=rule.id,
            device_id=network_device.id,
            metric_type=MetricType.cpu,
            measured_value=95.0,
            status=AlertStatus.acknowledged,
            severity=AlertSeverity.critical,
        )
    )
    db_session.flush()

    def metrics_must_not_run(_query):
        raise AssertionError("VictoriaMetrics must stay off the overview path")

    monkeypatch.setattr(web_monitoring, "_vm_instant_query", metrics_must_not_run)
    glance = web_monitoring.monitoring_glance(db_session)
    assert glance.alarms_open == 12
    assert glance.alarms_critical == 12
    assert glance.devices_total == 1


def test_overview_only_requests_glance(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/network/monitoring",
            "query_string": b"",
            "headers": [],
        }
    )
    glance = web_monitoring.MonitoringGlance(0, 0, 0, 0, 0, 0, 0, 0, datetime.now(UTC))
    monkeypatch.setattr(
        routes, "_base_context", lambda *_args, **_kwargs: {"request": request}
    )
    monkeypatch.setattr(web_monitoring, "monitoring_glance", lambda _db: glance)
    monkeypatch.setattr(
        web_monitoring,
        "monitoring_attention",
        lambda _db: (_ for _ in ()).throw(AssertionError("attention loaded eagerly")),
    )
    monkeypatch.setattr(
        web_monitoring,
        "monitoring_bandwidth_context",
        lambda _db: (_ for _ in ()).throw(AssertionError("bandwidth loaded eagerly")),
    )
    monkeypatch.setattr(
        routes.templates,
        "TemplateResponse",
        lambda name, context: (name, context),
    )
    name, context = routes.monitoring_page(request, db=object())
    assert name == "admin/network/monitoring/index.html"
    assert context["glance"] is glance


def test_overview_has_deferred_panels_and_separate_inventory():
    index = Path("templates/admin/network/monitoring/index.html").read_text(
        encoding="utf-8"
    )
    devices = Path("templates/admin/network/monitoring/devices.html").read_text(
        encoding="utf-8"
    )
    assert 'hx-trigger="load"' in index
    assert "/admin/network/monitoring/attention" in index
    assert "/admin/network/monitoring/pon-outages" in index
    assert "/admin/network/monitoring/bandwidth" in index
    assert 'include "admin/network/monitoring/_bandwidth_partial.html"' not in index
    assert 'hx-post="/admin/network/monitoring/bulk-action"' in devices
    assert 'action="/admin/network/monitoring/devices"' in devices


def test_legacy_inventory_query_redirects_to_devices():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/network/monitoring",
            "query_string": b"q=olt&page=2&per_page=25",
            "headers": [],
        }
    )
    response = routes.monitoring_page(request, db=object())
    assert response.status_code == 307
    assert response.headers["location"] == (
        "/admin/network/monitoring/devices?q=olt&page=2&per_page=25"
    )


def test_monitoring_links_have_registered_routes():
    files = [
        "index.html",
        "devices.html",
        "_kpi_partial.html",
        "_attention_partial.html",
        "_pon_outages_partial.html",
        "_infrastructure_partial.html",
    ]
    registered = (
        routes.router.routes
        + network_routes.router.routes
        + core_device_routes.router.routes
    )
    for name in files:
        markup = Path(f"templates/admin/network/monitoring/{name}").read_text(
            encoding="utf-8"
        )
        for attribute, url in re.findall(
            r'(href|hx-get|hx-post|action)="(/admin/network/[^"]+)"', markup
        ):
            path = re.sub(r"\{\{.*?\}\}", "00000000-0000-0000-0000-000000000001", url)
            path = path.removeprefix("/admin")
            method = "POST" if attribute == "hx-post" else "GET"
            assert any(
                route.path_regex.fullmatch(path) and method in route.methods
                for route in registered
            ), f"{name}: {method} {url} has no registered route"
