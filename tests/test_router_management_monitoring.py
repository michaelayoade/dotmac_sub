from app.models.router_management import Router, RouterStatus
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
