"""Tests for network monitoring service."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
)
from app.models.network_monitoring import (
    Alert,
    AlertOperator,
    AlertSeverity,
    AlertStatus,
    DeviceMetric,
    MetricType,
)
from app.models.system_user import SystemUser
from app.schemas.network_monitoring import (
    AlertAcknowledgeRequest,
    AlertResolveRequest,
    AlertRuleCreate,
    DeviceInterfaceCreate,
    DeviceMetricCreate,
    NetworkDeviceCreate,
    PopSiteCreate,
    PopSiteUpdate,
)
from app.services import monitoring_metrics as monitoring_metrics_service
from app.services import network_monitoring as monitoring_service
from app.services import web_network_monitoring as web_network_monitoring_service
from app.services.network import olt_polling_metrics as olt_polling_metrics_service
from app.tasks import alert_evaluation as alert_evaluation_task


def test_create_pop_site(db_session):
    """Test creating a POP site."""
    pop = monitoring_service.pop_sites.create(
        db_session,
        PopSiteCreate(
            name="Downtown POP",
            code="DT001",
            address_line1="123 Main St",
        ),
    )
    assert pop.name == "Downtown POP"
    assert pop.code == "DT001"


def test_update_pop_site(db_session):
    """Test updating a POP site."""
    pop = monitoring_service.pop_sites.create(
        db_session,
        PopSiteCreate(name="Original POP", code="ORIG"),
    )
    updated = monitoring_service.pop_sites.update(
        db_session,
        str(pop.id),
        PopSiteUpdate(name="Updated POP"),
    )
    assert updated.name == "Updated POP"


def test_list_pop_sites(db_session):
    """Test listing POP sites."""
    monitoring_service.pop_sites.create(
        db_session,
        PopSiteCreate(name="POP A", code="POPA"),
    )
    monitoring_service.pop_sites.create(
        db_session,
        PopSiteCreate(name="POP B", code="POPB"),
    )

    sites = monitoring_service.pop_sites.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(sites) >= 2


def test_create_network_device(db_session, pop_site):
    """Test creating a network device."""
    device = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Core Router",
            hostname="core-router-01",
            pop_site_id=pop_site.id,
            mgmt_ip="10.0.0.1",
        ),
    )
    assert device.name == "Core Router"
    assert device.pop_site_id == pop_site.id


def test_list_network_devices_by_pop(db_session, pop_site):
    """Test listing network devices by POP site."""
    monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Device A",
            hostname="device-a",
            pop_site_id=pop_site.id,
        ),
    )
    monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Device B",
            hostname="device-b",
            pop_site_id=pop_site.id,
        ),
    )

    devices = monitoring_service.network_devices.list(
        db_session,
        pop_site_id=str(pop_site.id),
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(devices) >= 2
    assert all(d.pop_site_id == pop_site.id for d in devices)


def test_onu_auth_trend_returns_json_safe_series(db_session):
    db_session.add_all(
        [
            OntUnit(serial_number="ONT-TREND-1"),
            OntUnit(serial_number="ONT-TREND-2"),
        ]
    )
    db_session.commit()

    trend = web_network_monitoring_service._get_onu_auth_trend(db_session, days=30)

    assert isinstance(trend["labels"], list)
    assert isinstance(trend["values"], list)
    assert all(isinstance(value, int) for value in trend["values"])
    json.dumps(trend["labels"])
    json.dumps(trend["values"])


def test_get_onu_status_summary_uses_effective_status(db_session):
    db_session.add_all(
        [
            OntUnit(
                serial_number="ONT-SUM-1",
                online_status=OnuOnlineStatus.offline,
                effective_status=OnuOnlineStatus.online,
            ),
            OntUnit(
                serial_number="ONT-SUM-2",
                online_status=OnuOnlineStatus.online,
                effective_status=OnuOnlineStatus.offline,
            ),
            OntUnit(
                serial_number="ONT-SUM-3",
                online_status=OnuOnlineStatus.unknown,
                effective_status=OnuOnlineStatus.unknown,
            ),
        ]
    )
    db_session.commit()

    summary = monitoring_service.get_onu_status_summary(db_session)

    assert summary["total"] == 3
    assert summary["online"] == 1
    assert summary["offline"] == 1


def test_get_onu_olt_status_summary_uses_raw_olt_status(db_session):
    db_session.add_all(
        [
            OntUnit(
                serial_number="ONT-OLT-SUM-1",
                online_status=OnuOnlineStatus.online,
                effective_status=OnuOnlineStatus.offline,
            ),
            OntUnit(
                serial_number="ONT-OLT-SUM-2",
                online_status=OnuOnlineStatus.offline,
                effective_status=OnuOnlineStatus.online,
            ),
            OntUnit(
                serial_number="ONT-OLT-SUM-3",
                online_status=OnuOnlineStatus.unknown,
                effective_status=OnuOnlineStatus.online,
            ),
        ]
    )
    db_session.commit()

    summary = monitoring_service.get_onu_olt_status_summary(db_session)

    assert summary["total"] == 3
    assert summary["online"] == 1
    assert summary["offline"] == 1
    assert summary["unknown"] == 1


def test_get_pon_outage_summary_only_flags_fully_offline_ports(db_session):
    olt = OLTDevice(name="SPDC Huawei OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    full_port = PonPort(olt_id=olt.id, name="pon-0/1/1")
    partial_port = PonPort(olt_id=olt.id, name="pon-0/1/2")
    db_session.add_all([full_port, partial_port])
    db_session.commit()
    db_session.refresh(full_port)
    db_session.refresh(partial_port)

    for idx in range(2):
        ont = OntUnit(
            serial_number=f"FULL-{idx}-{uuid.uuid4().hex[:8]}",
            online_status=OnuOnlineStatus.offline,
            effective_status=OnuOnlineStatus.offline,
        )
        db_session.add(ont)
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=full_port.id, active=True)
        )

    offline_partial = OntUnit(
        serial_number=f"PARTIAL-OFFLINE-{uuid.uuid4().hex[:8]}",
        online_status=OnuOnlineStatus.offline,
        effective_status=OnuOnlineStatus.offline,
    )
    online_partial = OntUnit(
        serial_number=f"PARTIAL-ONLINE-{uuid.uuid4().hex[:8]}",
        online_status=OnuOnlineStatus.online,
        effective_status=OnuOnlineStatus.online,
    )
    db_session.add_all([offline_partial, online_partial])
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=offline_partial.id,
                pon_port_id=partial_port.id,
                active=True,
            ),
            OntAssignment(
                ont_unit_id=online_partial.id,
                pon_port_id=partial_port.id,
                active=True,
            ),
        ]
    )
    db_session.commit()

    summary = monitoring_service.get_pon_outage_summary(db_session)

    assert len(summary) == 1
    assert summary[0]["pon_port_name"] == "pon-0/1/1"
    assert summary[0]["offline_count"] == 2
    assert summary[0]["total_count"] == 2


def test_get_onu_status_trend_includes_effective_and_raw_olt_series(monkeypatch):
    def _fake_vm_range_query(query: str, start, end, step):
        samples = {
            'onu_status_total{status="online"}': [["1712000000", "5"]],
            'onu_status_total{status="offline"}': [["1712000000", "2"]],
            'onu_olt_status_total{status="online"}': [["1712000000", "4"]],
            'onu_olt_status_total{status="offline"}': [["1712000000", "3"]],
            "sum(onu_signal_low)": [["1712000000", "1"]],
        }
        values = samples.get(query, [])
        return [{"values": values}] if values else []

    monkeypatch.setattr(
        web_network_monitoring_service,
        "_vm_range_query",
        _fake_vm_range_query,
    )

    trend = web_network_monitoring_service._get_onu_status_trend(hours=24)

    assert trend["has_data"] is True
    assert trend["online"] == [5.0]
    assert trend["offline"] == [2.0]
    assert trend["olt_online"] == [4.0]
    assert trend["olt_offline"] == [3.0]
    assert trend["low_signal"] == [1.0]


def test_push_signal_metrics_uses_effective_status_and_separate_olt_counts(
    db_session, monkeypatch
):
    captured: dict[str, str] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, content, headers):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(olt_polling_metrics_service.httpx, "Client", _FakeClient)

    db_session.add_all(
        [
            OntUnit(
                serial_number="ONT-METRIC-1",
                is_active=True,
                signal_updated_at=datetime.now(UTC),
                online_status=OnuOnlineStatus.offline,
                effective_status=OnuOnlineStatus.online,
            ),
            OntUnit(
                serial_number="ONT-METRIC-2",
                is_active=True,
                signal_updated_at=datetime.now(UTC),
                online_status=OnuOnlineStatus.online,
                effective_status=OnuOnlineStatus.offline,
            ),
        ]
    )
    db_session.commit()

    lines_written = olt_polling_metrics_service._push_signal_metrics(db_session)
    payload = captured["content"]

    assert lines_written > 0
    assert 'onu_status_total{status="online"} 1 ' in payload
    assert 'onu_status_total{status="offline"} 1 ' in payload
    assert 'onu_olt_status_total{status="online"} 1 ' in payload
    assert 'onu_olt_status_total{status="offline"} 1 ' in payload


def test_create_device_interface(db_session, network_device):
    """Test creating a device interface."""
    interface = monitoring_service.device_interfaces.create(
        db_session,
        DeviceInterfaceCreate(
            device_id=network_device.id,
            name="GigabitEthernet0/0",
        ),
    )
    assert interface.device_id == network_device.id
    assert interface.name == "GigabitEthernet0/0"


def test_create_device_metric(db_session, network_device):
    """Test creating a device metric."""
    now = datetime.now(UTC)
    metric = monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=network_device.id,
            metric_type=MetricType.cpu,
            value=45,
            recorded_at=now,
        ),
    )
    assert metric.device_id == network_device.id
    assert metric.metric_type == MetricType.cpu
    assert metric.value == 45


def test_create_alert_rule(db_session, network_device):
    """Test creating an alert rule."""
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="High CPU Alert",
            metric_type=MetricType.cpu,
            threshold=80.0,
            severity=AlertSeverity.warning,
            device_id=network_device.id,
        ),
    )
    assert rule.name == "High CPU Alert"
    assert rule.threshold == 80.0
    assert rule.severity == AlertSeverity.warning


def test_alert_triggered_by_metric(db_session, network_device):
    """Test that alerts are triggered when metrics violate rules."""
    # Create a rule
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Memory Alert",
            metric_type=MetricType.memory,
            threshold=90.0,
            severity=AlertSeverity.critical,
            device_id=network_device.id,
        ),
    )

    # Create a metric that violates the rule
    now = datetime.now(UTC)
    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=network_device.id,
            metric_type=MetricType.memory,
            value=95,  # Exceeds threshold of 90
            recorded_at=now,
        ),
    )

    # Check that an alert was created
    alerts = monitoring_service.alerts.list(
        db_session,
        rule_id=str(rule.id),
        device_id=None,
        interface_id=None,
        status=None,
        severity=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(alerts) >= 1
    assert alerts[0].status == AlertStatus.open


def test_alert_acknowledge(db_session, network_device):
    """Test acknowledging an alert."""
    # Create rule and trigger alert
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Test Rule",
            metric_type=MetricType.cpu,
            threshold=50.0,
            severity=AlertSeverity.info,
            device_id=network_device.id,
        ),
    )
    now = datetime.now(UTC)
    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=network_device.id,
            metric_type=MetricType.cpu,
            value=60,
            recorded_at=now,
        ),
    )

    # Get the alert
    alerts = monitoring_service.alerts.list(
        db_session,
        rule_id=str(rule.id),
        device_id=None,
        interface_id=None,
        status=None,
        severity=None,
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )
    assert len(alerts) >= 1
    alert = alerts[0]

    # Acknowledge the alert
    acknowledged = monitoring_service.alerts.acknowledge(
        db_session,
        str(alert.id),
        AlertAcknowledgeRequest(message="Acknowledged by admin"),
    )
    assert acknowledged.status == AlertStatus.acknowledged


def test_alert_resolve(db_session, network_device):
    """Test resolving an alert."""
    # Create rule and trigger alert
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Resolve Test",
            metric_type=MetricType.temperature,
            threshold=100.0,
            severity=AlertSeverity.warning,
            device_id=network_device.id,
        ),
    )
    now = datetime.now(UTC)
    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=network_device.id,
            metric_type=MetricType.temperature,
            value=150,
            recorded_at=now,
        ),
    )

    # Get the alert
    alerts = monitoring_service.alerts.list(
        db_session,
        rule_id=str(rule.id),
        device_id=None,
        interface_id=None,
        status=None,
        severity=None,
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )
    assert len(alerts) >= 1
    alert = alerts[0]

    # Resolve the alert
    resolved = monitoring_service.alerts.resolve(
        db_session,
        str(alert.id),
        AlertResolveRequest(message="Issue fixed"),
    )
    assert resolved.status == AlertStatus.resolved


def test_list_alerts_by_status(db_session, network_device):
    """Test listing alerts by status."""
    # Create rule and trigger alert
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Status Filter",
            metric_type=MetricType.rx_bps,
            threshold=1000.0,
            severity=AlertSeverity.warning,
            device_id=network_device.id,
        ),
    )
    now = datetime.now(UTC)
    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=network_device.id,
            metric_type=MetricType.rx_bps,
            value=1500,
            recorded_at=now,
        ),
    )

    open_alerts = monitoring_service.alerts.list(
        db_session,
        rule_id=None,
        device_id=None,
        interface_id=None,
        status=AlertStatus.open.value,
        severity=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(open_alerts) >= 1
    assert all(a.status == AlertStatus.open for a in open_alerts)


def test_list_alerts_by_severity(db_session, network_device):
    """Test listing alerts by severity."""
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Critical Alert",
            metric_type=MetricType.uptime,
            threshold=0.0,
            severity=AlertSeverity.critical,
            device_id=network_device.id,
        ),
    )
    now = datetime.now(UTC)
    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=network_device.id,
            metric_type=MetricType.uptime,
            value=1,  # Non-zero triggers > 0 threshold
            recorded_at=now,
        ),
    )

    critical_alerts = monitoring_service.alerts.list(
        db_session,
        rule_id=None,
        device_id=None,
        interface_id=None,
        status=None,
        severity=AlertSeverity.critical.value,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert all(a.severity == AlertSeverity.critical for a in critical_alerts)


def test_delete_network_device(db_session, pop_site):
    """Test deleting a network device."""
    device = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="To Delete",
            hostname="delete-me",
            pop_site_id=pop_site.id,
        ),
    )
    monitoring_service.network_devices.delete(db_session, str(device.id))
    db_session.refresh(device)
    assert device.is_active is False


def test_uptime_alert_respects_device_notification_delay(db_session, pop_site):
    device = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Delay Device",
            hostname="delay-device",
            pop_site_id=pop_site.id,
            send_notifications=True,
            notification_delay_minutes=5,
        ),
    )
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Delay Uptime Rule",
            metric_type=MetricType.uptime,
            operator=AlertOperator.lt,
            threshold=1.0,
            severity=AlertSeverity.warning,
            device_id=device.id,
        ),
    )
    t0 = datetime.now(UTC)
    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=device.id,
            metric_type=MetricType.uptime,
            value=0,
            recorded_at=t0,
        ),
    )
    alerts_initial = monitoring_service.alerts.list(
        db_session,
        rule_id=str(rule.id),
        device_id=str(device.id),
        interface_id=None,
        status=None,
        severity=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert alerts_initial == []

    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=device.id,
            metric_type=MetricType.uptime,
            value=0,
            recorded_at=t0 + timedelta(minutes=6),
        ),
    )
    alerts_after_delay = monitoring_service.alerts.list(
        db_session,
        rule_id=str(rule.id),
        device_id=str(device.id),
        interface_id=None,
        status=None,
        severity=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(alerts_after_delay) >= 1
    assert alerts_after_delay[0].status == AlertStatus.open


def test_alert_suppressed_when_device_notifications_disabled(db_session, pop_site):
    device = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Muted Device",
            hostname="muted-device",
            pop_site_id=pop_site.id,
            send_notifications=False,
            notification_delay_minutes=0,
        ),
    )
    rule = monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Muted CPU Rule",
            metric_type=MetricType.cpu,
            operator=AlertOperator.gt,
            threshold=10.0,
            severity=AlertSeverity.warning,
            device_id=device.id,
        ),
    )
    monitoring_service.device_metrics.create(
        db_session,
        DeviceMetricCreate(
            device_id=device.id,
            metric_type=MetricType.cpu,
            value=80,
            recorded_at=datetime.now(UTC),
        ),
    )
    alerts = monitoring_service.alerts.list(
        db_session,
        rule_id=str(rule.id),
        device_id=str(device.id),
        interface_id=None,
        status=None,
        severity=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert alerts == []


def test_delete_pop_site(db_session):
    """Test deleting a POP site."""
    pop = monitoring_service.pop_sites.create(
        db_session,
        PopSiteCreate(name="To Delete POP", code="DEL"),
    )
    monitoring_service.pop_sites.delete(db_session, str(pop.id))
    db_session.refresh(pop)
    assert pop.is_active is False


def test_list_alert_rules(db_session, network_device):
    """Test listing alert rules."""
    monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Rule 1",
            metric_type=MetricType.cpu,
            threshold=70.0,
            severity=AlertSeverity.warning,
        ),
    )
    monitoring_service.alert_rules.create(
        db_session,
        AlertRuleCreate(
            name="Rule 2",
            metric_type=MetricType.memory,
            threshold=80.0,
            severity=AlertSeverity.critical,
        ),
    )

    rules = monitoring_service.alert_rules.list(
        db_session,
        metric_type=None,
        device_id=None,
        interface_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(rules) >= 2


def test_get_device_health_table_uses_latest_metrics(db_session, network_device):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            DeviceMetric(
                device_id=network_device.id,
                metric_type=MetricType.cpu,
                value=55,
                recorded_at=now - timedelta(minutes=5),
            ),
            DeviceMetric(
                device_id=network_device.id,
                metric_type=MetricType.cpu,
                value=71,
                recorded_at=now,
            ),
            DeviceMetric(
                device_id=network_device.id,
                metric_type=MetricType.memory,
                value=84,
                recorded_at=now,
            ),
        ]
    )
    db_session.commit()

    rows = web_network_monitoring_service._get_device_health_table(db_session)

    row = next(item for item in rows if item["id"] == str(network_device.id))
    assert row["ip"] == str(network_device.mgmt_ip or "")
    assert row["cpu"] == 71.0
    assert row["memory"] == 84.0


def test_poll_device_system_metrics_uses_mgmt_ip_backed_device(db_session, network_device, monkeypatch):
    network_device.mgmt_ip = "10.0.0.9"
    network_device.vendor = "mikrotik"
    network_device.snmp_community = "encrypted-community"
    db_session.commit()

    oid_values = {
        ".1.3.6.1.2.1.25.3.3.1.2.1": 63.0,
        ".1.3.6.1.2.1.25.2.3.1.6.65536": 400.0,
        ".1.3.6.1.2.1.25.2.3.1.5.65536": 800.0,
        ".1.3.6.1.2.1.1.3.0": 12345.0,
        ".1.3.6.1.4.1.14988.1.1.3.10.0": 48.0,
    }

    monkeypatch.setattr(
        monitoring_metrics_service,
        "_snmp_get_single",
        lambda device, oid: oid_values.get(oid),
    )
    pushed: dict[str, float] = {}
    monkeypatch.setattr(
        monitoring_metrics_service,
        "push_device_health_metrics",
        lambda device, metrics: pushed.update(metrics),
    )

    result = monitoring_metrics_service.poll_device_system_metrics(db_session, network_device)
    db_session.commit()

    assert result["stored"] == 4
    assert pushed["cpu"] == 63.0
    assert pushed["memory"] == 50.0
    assert pushed["temperature"] == 48.0


def test_poll_onu_signal_strength_delegates_to_olt_inventory(db_session, network_device, monkeypatch):
    network_device.mgmt_ip = "10.20.30.40"
    network_device.vendor = "huawei"
    db_session.add(
        OLTDevice(
            name="OLT-1",
            mgmt_ip="10.20.30.40",
            vendor="huawei",
            is_active=True,
            snmp_ro_community="enc-ro",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.monitoring_metrics.decrypt_credential",
        lambda value: "public",
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.network.olt_polling.poll_olt_ont_signals",
        lambda db, olt, community=None: {"polled": 12, "updated": 9, "errors": 1},
    )
    monkeypatch.setattr(
        "app.services.network.olt_polling.push_signal_metrics_to_victoriametrics",
        lambda db: 5,
    )
    monkeypatch.setattr(
        "app.services.network.olt_polling.get_signal_thresholds",
        lambda db: (-25.0, -28.0),
    )

    result = monitoring_metrics_service.poll_onu_signal_strength(db_session, network_device)

    assert result["polled"] == 12
    assert result["stored"] == 9
    assert result["errors"] == 1


def test_monitoring_config_context_includes_runtime_settings(db_session):
    from app.services.web_system_config import get_monitoring_config_context

    context = get_monitoring_config_context(db_session)

    assert context["monitoring"]["device_metrics_retention_days"] == "90"
    assert context["monitoring"]["alert_evaluation_interval_seconds"] == "60"
    assert context["monitoring"]["interface_walk_interval_seconds"] == "300"
    assert context["monitoring"]["hot_retention_hours"] == "24"


def test_notify_alert_uses_policy_engine_before_admin_fallback(
    db_session,
    network_device,
    monkeypatch,
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
    alert = Alert(
        rule_id=rule.id,
        device_id=network_device.id,
        metric_type=MetricType.cpu,
        measured_value=95.0,
        status=AlertStatus.open,
        severity=AlertSeverity.critical,
    )
    db_session.add(alert)
    metric = DeviceMetric(
        device_id=network_device.id,
        metric_type=MetricType.cpu,
        value=95,
        recorded_at=datetime.now(UTC),
    )
    db_session.add(metric)
    db_session.add(
        SystemUser(
            first_name="Admin",
            last_name="User",
            email="admin-monitor@example.com",
            is_active=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.notification.alert_notification_policies.emit_for_alert",
        lambda db, alert, status: 2,
    )

    alert_evaluation_task._notify_alert(db_session, alert, rule, metric, action="triggered")
    db_session.commit()

    from app.models.notification import Notification

    queued = db_session.query(Notification).all()
    assert queued == []
