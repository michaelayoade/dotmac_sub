from datetime import UTC, datetime

from app.models.network_monitoring import (
    DeviceMetric,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    MetricType,
    NetworkDevice,
)
from app.services import web_network_core_devices_views as core_devices_views


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
