from app.models.network_monitoring import DeviceStatus, DeviceType, NetworkDevice
from app.models.router_management import Router, RouterStatus
from app.services.monitoring_metrics import (
    sync_all_routers_to_monitoring,
    sync_router_to_monitoring,
)
from app.services.router_management.monitoring import RouterMonitoringService


def _make_routers(db_session, count: int) -> list[Router]:
    routers = []
    for i in range(count):
        r = Router(
            name=f"mon-router-{i}",
            hostname=f"mr{i}",
            management_ip=f"10.0.{i}.1",
            rest_api_username="admin",
            rest_api_password="enc:test",
            status=RouterStatus.online if i % 2 == 0 else RouterStatus.offline,
        )
        db_session.add(r)
        routers.append(r)
    db_session.commit()
    for r in routers:
        db_session.refresh(r)
    return routers


def test_dashboard_summary(db_session):
    _make_routers(db_session, 4)
    summary = RouterMonitoringService.get_dashboard_summary(db_session)
    assert summary["total"] >= 4
    assert "online" in summary
    assert "offline" in summary
    assert "degraded" in summary
    assert "maintenance" in summary
    assert "unreachable" in summary


def test_dashboard_summary_empty(db_session):
    summary = RouterMonitoringService.get_dashboard_summary(db_session)
    assert summary["total"] >= 0
    assert summary["online"] >= 0


def test_sync_router_to_monitoring_creates_network_device(db_session):
    router = Router(
        name="monitoring-link-router",
        hostname="mlr",
        management_ip="10.250.0.1",
        rest_api_username="admin",
        rest_api_password="enc:test",
        status=RouterStatus.online,
        board_name="CCR2004",
        serial_number="SN-MON-1",
    )
    db_session.add(router)
    db_session.commit()

    device = sync_router_to_monitoring(db_session, str(router.id))
    db_session.commit()
    db_session.refresh(router)

    assert router.network_device_id == device.id
    assert device.name == "monitoring-link-router"
    assert device.hostname == "mlr"
    assert device.mgmt_ip == "10.250.0.1"
    assert device.device_type == DeviceType.router
    assert device.status == DeviceStatus.online
    assert device.ping_enabled is True
    assert device.vendor == "mikrotik"


def test_sync_router_to_monitoring_reuses_existing_device_by_ip(db_session):
    existing = NetworkDevice(
        name="Existing monitor",
        hostname="existing",
        mgmt_ip="10.250.0.2",
        device_type=DeviceType.router,
    )
    router = Router(
        name="monitoring-reuse-router",
        hostname="mrr",
        management_ip="10.250.0.2",
        rest_api_username="admin",
        rest_api_password="enc:test",
        status=RouterStatus.unreachable,
    )
    db_session.add_all([existing, router])
    db_session.commit()

    device = sync_router_to_monitoring(db_session, str(router.id))
    db_session.commit()
    db_session.refresh(router)

    assert device.id == existing.id
    assert router.network_device_id == existing.id
    assert device.name == "monitoring-reuse-router"
    assert device.status == DeviceStatus.offline


def test_sync_router_to_monitoring_relinks_when_existing_link_has_wrong_ip(db_session):
    wrong = NetworkDevice(
        name="Wrong monitor",
        hostname="wrong",
        mgmt_ip="10.250.0.40",
        device_type=DeviceType.router,
    )
    right = NetworkDevice(
        name="Right monitor",
        hostname="right",
        mgmt_ip="10.250.0.41",
        device_type=DeviceType.router,
    )
    db_session.add_all([wrong, right])
    db_session.flush()
    router = Router(
        name="monitoring-relink-router",
        hostname="mrel",
        management_ip="10.250.0.41",
        rest_api_username="admin",
        rest_api_password="enc:test",
        status=RouterStatus.online,
        network_device_id=wrong.id,
    )
    db_session.add(router)
    db_session.commit()

    device = sync_router_to_monitoring(db_session, str(router.id))
    db_session.commit()
    db_session.refresh(router)

    assert device.id == right.id
    assert router.network_device_id == right.id
    assert device.name == "monitoring-relink-router"


def test_sync_all_routers_to_monitoring_backfills_unlinked_routers(db_session):
    router = Router(
        name="monitoring-backfill-router",
        hostname="mbr",
        management_ip="10.250.0.3",
        rest_api_username="admin",
        rest_api_password="enc:test",
        status=RouterStatus.degraded,
    )
    db_session.add(router)
    db_session.commit()

    result = sync_all_routers_to_monitoring(db_session)
    db_session.refresh(router)

    assert result["synced"] >= 1
    assert router.network_device_id is not None
