from datetime import datetime, timedelta, timezone

from app.models.network_monitoring import Alert, AlertRule, MetricType, NetworkDevice, PopSite
from app.schemas.network_monitoring import UptimeReportRequest
from app.services import network_monitoring as monitoring_service


def test_uptime_report_by_device(db_session):
    pop_site = PopSite(name="Core POP")
    db_session.add(pop_site)
    db_session.flush()
    device = NetworkDevice(name="Edge 1", pop_site_id=pop_site.id)
    db_session.add(device)
    db_session.flush()
    rule = AlertRule(name="Uptime", metric_type=MetricType.uptime, threshold=0)
    db_session.add(rule)
    db_session.flush()
    period_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    period_end = period_start + timedelta(hours=1)
    alert = Alert(
        rule_id=rule.id,
        device_id=device.id,
        metric_type=MetricType.uptime,
        measured_value=0,
        triggered_at=period_start + timedelta(minutes=10),
        resolved_at=period_start + timedelta(minutes=25),
    )
    db_session.add(alert)
    db_session.commit()

    report = monitoring_service.uptime_report(
        db_session,
        UptimeReportRequest(
            period_start=period_start, period_end=period_end, group_by="device"
        ),
    )
    assert report.items
    item = report.items[0]
    assert item.device_count == 1
    assert item.downtime_seconds == 900
    assert float(item.uptime_percent) == 75.0
