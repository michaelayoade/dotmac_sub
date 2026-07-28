from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_connection_health_web_surfaces_consume_server_semantics() -> None:
    customer = _read("templates/customer/connection/index.html")
    reseller = _read("templates/reseller/dashboard/index.html")

    assert "this.status?.status_presentation?.tone" in customer
    assert "status.state === 'connected'" not in customer
    assert "status.state === 'trouble'" not in customer
    assert "status.state === 'outage'" not in customer

    assert "status_presentation_badge(row.status_presentation" in reseller
    assert "customer_statuses.count_presentations" in reseller
    assert "{% if state == 'connected' %}" not in reseller


def test_connection_health_mobile_parses_and_renders_server_semantics() -> None:
    model = _read("mobile/lib/src/models/connection_status.dart")
    screen = _read("mobile/lib/src/features/service/connection_status_screen.dart")
    dashboard = _read("mobile/lib/src/features/home/dashboard_screen.dart")

    assert "json['status_presentation']" in model
    assert "StatusPresentation.neutralFallback" not in model
    assert "required this.statusPresentation" in model
    assert "statusPresentationVisual(context, status.statusPresentation)" in screen
    assert "statusPresentationVisual(context, c.statusPresentation)" in dashboard
    assert "ConnectionHealth.connected =>" not in screen
    assert "c.state == ConnectionHealth" not in dashboard


def test_portal_service_health_uses_the_shared_server_projection() -> None:
    service = _read("app/services/customer_portal_flow_services.py")
    template = _read("templates/customer/services/detail.html")

    assert "build_portal_account_health" in service
    assert "def _radius_connection_status" not in service
    assert "connection_health_status_presentation" not in service
    assert "service_health_strip(account_health" in template
    assert "connection_status.state" not in template
