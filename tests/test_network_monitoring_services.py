"""Tests for network monitoring service."""

from datetime import datetime, timezone

from app.models.network_monitoring import MetricType, AlertStatus, AlertSeverity
from app.schemas.network_monitoring import (
    PopSiteCreate, PopSiteUpdate,
    NetworkDeviceCreate, NetworkDeviceUpdate,
    DeviceInterfaceCreate,
    DeviceMetricCreate,
    AlertRuleCreate,
    AlertAcknowledgeRequest,
    AlertResolveRequest,
)
from app.services import network_monitoring as monitoring_service


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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
