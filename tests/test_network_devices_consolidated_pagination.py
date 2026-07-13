from datetime import UTC, datetime
from pathlib import Path

from app.models.network_monitoring import (
    DeviceMetric,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    MetricType,
    NetworkDevice,
)
from app.services import web_network_core_devices_views as core_devices_views
from app.web.admin import network_core_devices as core_devices_web


def test_paginate_rows_clamps_stale_pages_and_handles_empty_lists():
    rows, pagination = core_devices_web._paginate_rows(list(range(7)), 99, 3)

    assert rows == [6]
    assert pagination == {
        "page": 3,
        "per_page": 3,
        "total": 7,
        "total_pages": 3,
        "has_prev": True,
        "has_next": False,
    }

    rows, pagination = core_devices_web._paginate_rows([], 4, 25)

    assert rows == []
    assert pagination["page"] == 1
    assert pagination["total_pages"] == 1


def test_consolidated_template_uses_server_tabs_and_independent_pagination():
    template = Path("templates/admin/network/network-devices/index.html").read_text()

    assert "activeTab" not in template
    assert "{% if tab == 'core' %}" in template
    assert "{% if tab == 'olts' %}" in template
    assert "{% if tab == 'onts' %}" in template
    assert "include_query_params(olt_page=" in template
    assert "include_query_params(ont_page=" in template
    assert "include_query_params(cpe_page=" in template


def test_consolidated_page_data_does_not_cap_core_devices_before_pagination(
    db_session,
):
    devices = [
        NetworkDevice(
            name=f"Core Switch {idx:03d}",
            hostname=f"core-switch-{idx:03d}.local",
            mgmt_ip=f"198.51.100.{idx % 250}",
            device_type=DeviceType.switch,
            role=DeviceRole.distribution,
            status=DeviceStatus.online,
            is_active=True,
        )
        for idx in range(205)
    ]
    db_session.add_all(devices)
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(tab="core", db=db_session)

    names = {device.name for device in payload["core_devices"]}
    assert len(names) == 205
    assert "Core Switch 204" in names
    assert payload["stats"]["core_total"] == 205


def test_consolidated_page_data_includes_core_table_maps(db_session):
    device = NetworkDevice(
        name="Core Table Router",
        hostname="core-table-router.local",
        mgmt_ip="198.51.100.254",
        device_type=DeviceType.router,
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()
    db_session.add(
        DeviceMetric(
            device_id=device.id,
            metric_type=MetricType.uptime,
            value=3661,
            unit="seconds",
            recorded_at=datetime.now(UTC),
        )
    )
    db_session.add(
        DeviceMetric(
            device_id=device.id,
            metric_type=MetricType.custom,
            value=8,
            unit="ping_ms",
            recorded_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(tab="core", db=db_session)
    key = str(device.id)

    assert payload["display_status_map"][key] == "online"
    assert payload["uptime_map"][key] == "1h 1m"
    assert payload["ping_history_map"][key][0]["ok"] is True
