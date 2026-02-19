"""Service helpers for admin network monitoring web routes."""

from __future__ import annotations

from sqlalchemy import and_, func
from sqlalchemy.orm import Session


def monitoring_page_data(db: Session, *, format_duration, format_bps) -> dict[str, object]:
    """Return payload for network monitoring dashboard."""
    from app.models.network_monitoring import (
        Alert,
        AlertEvent,
        AlertStatus,
        DeviceMetric,
        DeviceStatus,
        MetricType,
        NetworkDevice,
    )
    from app.models.usage import AccountingStatus, RadiusAccountingSession

    devices = (
        db.query(NetworkDevice)
        .filter(NetworkDevice.is_active.is_(True))
        .order_by(NetworkDevice.name)
        .all()
    )
    online_statuses = {DeviceStatus.online, DeviceStatus.degraded, DeviceStatus.maintenance}
    devices_online = sum(1 for d in devices if d.status in online_statuses)
    devices_offline = sum(1 for d in devices if d.status == DeviceStatus.offline)

    alerts = (
        db.query(Alert)
        .filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
        .order_by(Alert.triggered_at.desc())
        .limit(10)
        .all()
    )
    active_alarm_count = sum(1 for a in alerts if a.status == AlertStatus.open)

    recent_events = (
        db.query(AlertEvent)
        .order_by(AlertEvent.created_at.desc())
        .limit(10)
        .all()
    )

    online_subscribers = (
        db.query(func.count(func.distinct(RadiusAccountingSession.subscription_id)))
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .scalar()
        or 0
    )

    metric_types = [MetricType.cpu, MetricType.memory, MetricType.uptime, MetricType.rx_bps, MetricType.tx_bps]
    latest_metrics_subq = (
        db.query(
            DeviceMetric.device_id,
            DeviceMetric.metric_type,
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .filter(DeviceMetric.metric_type.in_(metric_types))
        .group_by(DeviceMetric.device_id, DeviceMetric.metric_type)
        .subquery()
    )
    latest_metrics = (
        db.query(DeviceMetric)
        .join(
            latest_metrics_subq,
            and_(
                DeviceMetric.device_id == latest_metrics_subq.c.device_id,
                DeviceMetric.metric_type == latest_metrics_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_metrics_subq.c.latest,
            ),
        )
        .all()
    )

    metrics_by_device: dict[str, dict[MetricType, DeviceMetric]] = {}
    rx_total = 0.0
    tx_total = 0.0
    cpu_values = []
    mem_values = []
    for metric in latest_metrics:
        device_metrics = metrics_by_device.setdefault(str(metric.device_id), {})
        device_metrics[metric.metric_type] = metric
        if metric.metric_type == MetricType.rx_bps:
            rx_total += float(metric.value or 0)
        elif metric.metric_type == MetricType.tx_bps:
            tx_total += float(metric.value or 0)
        elif metric.metric_type == MetricType.cpu:
            cpu_values.append(float(metric.value or 0))
        elif metric.metric_type == MetricType.memory:
            mem_values.append(float(metric.value or 0))

    avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else None
    avg_mem = sum(mem_values) / len(mem_values) if mem_values else None

    device_health = []
    for device in devices:
        device_metrics = metrics_by_device.get(str(device.id), {})
        cpu_metric = device_metrics.get(MetricType.cpu)
        mem_metric = device_metrics.get(MetricType.memory)
        uptime_metric = device_metrics.get(MetricType.uptime)
        device_health.append(
            {
                "name": device.name,
                "status": device.status.value if device.status else "unknown",
                "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
                "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
                "uptime": format_duration(uptime_metric.value if uptime_metric else None),
                "last_seen": device.last_ping_at or device.last_snmp_at,
            }
        )

    return {
        "stats": {
            "devices_online": devices_online,
            "devices_offline": devices_offline,
            "alarms_open": active_alarm_count,
            "subscribers_online": online_subscribers,
        },
        "alarms": alerts,
        "recent_events": recent_events,
        "performance": {
            "avg_cpu": f"{avg_cpu:.1f}%" if avg_cpu is not None else "--",
            "avg_memory": f"{avg_mem:.1f}%" if avg_mem is not None else "--",
            "rx_bps": format_bps(rx_total) if rx_total > 0 else "--",
            "tx_bps": format_bps(tx_total) if tx_total > 0 else "--",
        },
        "device_health": device_health,
    }


def alarms_page_data(db: Session, *, severity: str | None, status: str | None) -> dict[str, object]:
    """Return payload for monitoring alarms page."""
    from app.models.network_monitoring import Alert, AlertRule, AlertSeverity, AlertStatus

    alarms_query = db.query(Alert).order_by(Alert.triggered_at.desc())
    if status:
        try:
            alarms_query = alarms_query.filter(Alert.status == AlertStatus(status))
        except ValueError:
            pass
    else:
        alarms_query = alarms_query.filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
    if severity:
        try:
            alarms_query = alarms_query.filter(Alert.severity == AlertSeverity(severity))
        except ValueError:
            pass
    alarms = alarms_query.limit(100).all()
    rules = db.query(AlertRule).filter(AlertRule.is_active.is_(True)).order_by(AlertRule.name).all()
    stats = {
        "critical": sum(1 for a in alarms if a.severity == AlertSeverity.critical and a.status == AlertStatus.open),
        "warning": sum(1 for a in alarms if a.severity == AlertSeverity.warning and a.status == AlertStatus.open),
        "info": sum(1 for a in alarms if a.severity == AlertSeverity.info and a.status == AlertStatus.open),
        "total_open": sum(1 for a in alarms if a.status == AlertStatus.open),
    }
    return {
        "alarms": alarms,
        "rules": rules,
        "stats": stats,
        "severity": severity,
        "status": status,
    }
