from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import cast

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    Alert,
    AlertEvent,
    AlertOperator,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    DeviceInterface,
    DeviceMetric,
    MetricType,
    NetworkDevice,
    PopSite,
)
from app.schemas.network_monitoring import (
    AlertAcknowledgeRequest,
    AlertResolveRequest,
    AlertRuleBulkUpdateRequest,
    AlertRuleCreate,
    AlertRuleUpdate,
    DeviceInterfaceCreate,
    DeviceInterfaceUpdate,
    DeviceMetricCreate,
    DeviceMetricUpdate,
    NetworkDeviceCreate,
    NetworkDeviceUpdate,
    PopSiteCreate,
    PopSiteUpdate,
    UptimeReportItem,
    UptimeReportRequest,
    UptimeReportResponse,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.response import ListResponseMixin


def _round_percent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals.sort(key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _alert_intervals_by_device(
    db: Session, period_start: datetime, period_end: datetime
) -> dict[str, list[tuple[datetime, datetime]]]:
    period_start_utc = cast(datetime, _as_utc(period_start))
    period_end_utc = cast(datetime, _as_utc(period_end))
    alerts = (
        db.query(Alert)
        .filter(Alert.metric_type == MetricType.uptime)
        .filter(Alert.triggered_at <= period_end_utc)
        .filter((Alert.resolved_at.is_(None)) | (Alert.resolved_at >= period_start_utc))
        .all()
    )
    intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    for alert in alerts:
        if not alert.device_id:
            continue
        triggered_at = _as_utc(alert.triggered_at)
        resolved_at = _as_utc(alert.resolved_at)
        if triggered_at is None:
            continue
        start = max(triggered_at, period_start_utc)
        end = min(resolved_at or period_end_utc, period_end_utc)
        if end <= start:
            continue
        key = str(alert.device_id)
        intervals.setdefault(key, []).append((start, end))
    return intervals


def uptime_report(db: Session, payload: UptimeReportRequest) -> UptimeReportResponse:
    if payload.period_end <= payload.period_start:
        raise HTTPException(status_code=400, detail="period_end must be after period_start")
    window_seconds = int((payload.period_end - payload.period_start).total_seconds())
    devices = (
        db.query(NetworkDevice)
        .options(selectinload(NetworkDevice.pop_site))
        .filter(NetworkDevice.is_active.is_(True))
        .all()
    )
    downtime_intervals = _alert_intervals_by_device(
        db, payload.period_start, payload.period_end
    )
    downtime_by_device: dict[str, int] = {}
    for device_id, intervals in downtime_intervals.items():
        merged = _merge_intervals(intervals)
        total = sum(int((end - start).total_seconds()) for start, end in merged)
        downtime_by_device[device_id] = total
    items: list[UptimeReportItem] = []
    group_by = payload.group_by
    if group_by == "device":
        for device in devices:
            downtime = downtime_by_device.get(str(device.id), 0)
            uptime_percent = _round_percent(
                (Decimal(window_seconds - downtime) * Decimal("100.00"))
                / Decimal(window_seconds)
            )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=device.id,
                    name=device.name,
                    device_count=1,
                    total_seconds=window_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "pop_site":
        grouped: dict[str, list[NetworkDevice]] = {}
        for device in devices:
            key = str(device.pop_site_id) if device.pop_site_id else "unknown"
            grouped.setdefault(key, []).append(device)
        for key, grouped_devices in grouped.items():
            device_count = len(grouped_devices)
            total_seconds = window_seconds * device_count
            downtime = sum(
                downtime_by_device.get(str(device.id), 0) for device in grouped_devices
            )
            pop_site = grouped_devices[0].pop_site if grouped_devices else None
            name = pop_site.name if pop_site else "Unknown"
            uptime_percent = None
            if total_seconds > 0:
                uptime_percent = _round_percent(
                    (Decimal(total_seconds - downtime) * Decimal("100.00"))
                    / Decimal(total_seconds)
                )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=grouped_devices[0].pop_site_id if grouped_devices else None,
                    name=name,
                    device_count=device_count,
                    total_seconds=total_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "area":
        grouped_by_region: dict[str, list[NetworkDevice]] = {}
        for device in devices:
            region = device.pop_site.region if device.pop_site else None
            key = region or "Unknown"
            grouped_by_region.setdefault(key, []).append(device)
        for region, grouped_devices in grouped_by_region.items():
            device_count = len(grouped_devices)
            total_seconds = window_seconds * device_count
            downtime = sum(
                downtime_by_device.get(str(device.id), 0) for device in grouped_devices
            )
            uptime_percent = None
            if total_seconds > 0:
                uptime_percent = _round_percent(
                    (Decimal(total_seconds - downtime) * Decimal("100.00"))
                    / Decimal(total_seconds)
                )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=None,
                    name=region,
                    device_count=device_count,
                    total_seconds=total_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "fdh":
        region_to_devices: dict[str, list[NetworkDevice]] = {}
        for device in devices:
            region = device.pop_site.region if device.pop_site else None
            if not region:
                continue
            region_to_devices.setdefault(region, []).append(device)
        fdh_list = (
            db.query(FdhCabinet)
            .options(selectinload(FdhCabinet.region))
            .filter(FdhCabinet.is_active.is_(True))
            .all()
        )
        for fdh in fdh_list:
            region_name = fdh.region.name if fdh.region else None
            grouped_devices = region_to_devices.get(region_name or "", [])
            device_count = len(grouped_devices)
            total_seconds = window_seconds * device_count
            downtime = sum(
                downtime_by_device.get(str(device.id), 0) for device in grouped_devices
            )
            uptime_percent = None
            if total_seconds > 0:
                uptime_percent = _round_percent(
                    (Decimal(total_seconds - downtime) * Decimal("100.00"))
                    / Decimal(total_seconds)
                )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=fdh.id,
                    name=fdh.name,
                    device_count=device_count,
                    total_seconds=total_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    else:
        raise HTTPException(status_code=400, detail="Invalid group_by")
    return UptimeReportResponse(
        period_start=payload.period_start,
        period_end=payload.period_end,
        group_by=group_by,
        items=items,
    )


def _operator_check(operator: AlertOperator, measured: float, threshold: float) -> bool:
    if operator == AlertOperator.gt:
        return measured > threshold
    if operator == AlertOperator.gte:
        return measured >= threshold
    if operator == AlertOperator.lt:
        return measured < threshold
    if operator == AlertOperator.lte:
        return measured <= threshold
    return measured == threshold


def _rule_matches(rule: AlertRule, metric: DeviceMetric) -> bool:
    if rule.metric_type != metric.metric_type:
        return False
    if rule.device_id and rule.device_id != metric.device_id:
        return False
    if rule.interface_id and rule.interface_id != metric.interface_id:
        return False
    return True


def _violates_rule(db: Session, rule: AlertRule, metric: DeviceMetric) -> bool:
    if rule.duration_seconds and metric.recorded_at:
        window_start = metric.recorded_at - timedelta(seconds=rule.duration_seconds)
        query = (
            db.query(DeviceMetric)
            .filter(DeviceMetric.metric_type == rule.metric_type)
            .filter(DeviceMetric.device_id == metric.device_id)
            .filter(DeviceMetric.recorded_at >= window_start)
        )
        if metric.interface_id:
            query = query.filter(DeviceMetric.interface_id == metric.interface_id)
        samples = query.all()
        if not samples:
            return False
        violations = [
            sample
            for sample in samples
            if _operator_check(rule.operator, float(sample.value), float(rule.threshold))
        ]
        return len(violations) == len(samples)
    return _operator_check(rule.operator, float(metric.value), float(rule.threshold))


def _process_alerts(db: Session, metric: DeviceMetric) -> None:
    rules = (
        db.query(AlertRule)
        .filter(AlertRule.is_active.is_(True))
        .all()
    )
    for rule in rules:
        if not _rule_matches(rule, metric):
            continue
        violated = _violates_rule(db, rule, metric)
        existing = (
            db.query(Alert)
            .filter(Alert.rule_id == rule.id)
            .filter(Alert.device_id == metric.device_id)
            .filter(Alert.interface_id == metric.interface_id)
            .filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
            .first()
        )
        if violated:
            if existing:
                existing.measured_value = float(metric.value)
                continue
            alert = Alert(
                rule_id=rule.id,
                device_id=metric.device_id,
                interface_id=metric.interface_id,
                metric_type=metric.metric_type,
                measured_value=float(metric.value),
                status=AlertStatus.open,
                severity=rule.severity or AlertSeverity.warning,
                triggered_at=metric.recorded_at or datetime.now(UTC),
            )
            db.add(alert)
            db.flush()
            event = AlertEvent(
                alert_id=alert.id,
                status=AlertStatus.open,
                message="Alert triggered",
            )
            db.add(event)
            from app.services import notification as notification_service

            notification_service.alert_notification_policies.emit_for_alert(
                db, alert, AlertStatus.open
            )
        else:
            if existing:
                existing.status = AlertStatus.resolved
                existing.resolved_at = datetime.now(UTC)
                event = AlertEvent(
                    alert_id=existing.id,
                    status=AlertStatus.resolved,
                    message="Alert resolved",
                )
                db.add(event)
                from app.services import notification as notification_service

                notification_service.alert_notification_policies.emit_for_alert(
                    db, existing, AlertStatus.resolved
                )


class PopSites(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PopSiteCreate):
        site = PopSite(**payload.model_dump())
        db.add(site)
        db.commit()
        db.refresh(site)
        return site

    @staticmethod
    def get(db: Session, site_id: str):
        site = db.get(PopSite, site_id)
        if not site:
            raise HTTPException(status_code=404, detail="PoP site not found")
        return site

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PopSite)
        if is_active is None:
            query = query.filter(PopSite.is_active.is_(True))
        else:
            query = query.filter(PopSite.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PopSite.created_at, "name": PopSite.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, site_id: str, payload: PopSiteUpdate):
        site = db.get(PopSite, site_id)
        if not site:
            raise HTTPException(status_code=404, detail="PoP site not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(site, key, value)
        db.commit()
        db.refresh(site)
        return site

    @staticmethod
    def delete(db: Session, site_id: str):
        site = db.get(PopSite, site_id)
        if not site:
            raise HTTPException(status_code=404, detail="PoP site not found")
        site.is_active = False
        db.commit()


class NetworkDevices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: NetworkDeviceCreate):
        device = NetworkDevice(**payload.model_dump())
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str):
        device = db.get(NetworkDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Network device not found")
        return device

    @staticmethod
    def list(
        db: Session,
        pop_site_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(NetworkDevice)
        if pop_site_id:
            query = query.filter(NetworkDevice.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.filter(NetworkDevice.is_active.is_(True))
        else:
            query = query.filter(NetworkDevice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": NetworkDevice.created_at, "name": NetworkDevice.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, device_id: str, payload: NetworkDeviceUpdate):
        device = db.get(NetworkDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Network device not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(device, key, value)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str):
        device = db.get(NetworkDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Network device not found")
        device.is_active = False
        db.commit()

    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Build network dashboard stats for admin overview.

        Returns:
            Dictionary with online/total counts, uptime_percentage,
            active_alarms, device_status_chart, and alarm severity breakdown.
        """
        from app.models.network_monitoring import AlertSeverity as Sev
        from app.models.network_monitoring import AlertStatus as AStatus
        from app.models.network_monitoring import DeviceStatus as DStatus

        active_devices = (
            db.query(NetworkDevice)
            .filter(NetworkDevice.is_active.is_(True))
            .all()
        )
        total_count = len(active_devices)
        online_count = sum(
            1 for d in active_devices if d.status == DStatus.online
        )
        degraded_count = sum(
            1 for d in active_devices if d.status == DStatus.degraded
        )
        offline_count = sum(
            1 for d in active_devices if d.status == DStatus.offline
        )
        maintenance_count = sum(
            1 for d in active_devices if d.status == DStatus.maintenance
        )

        uptime_percentage = (
            round(
                (online_count + degraded_count + maintenance_count)
                / total_count
                * 100,
                1,
            )
            if total_count > 0
            else 0.0
        )

        # Device status chart
        device_status_chart = {
            "labels": ["Online", "Degraded", "Offline", "Maintenance"],
            "values": [online_count, degraded_count, offline_count, maintenance_count],
            "colors": ["#10b981", "#f59e0b", "#f43f5e", "#94a3b8"],
        }

        # Active alarms by severity (AlertStatus: open/acknowledged/resolved)
        open_alarms = (
            db.query(Alert)
            .filter(Alert.status == AStatus.open)
            .all()
        )
        alarms_critical = sum(
            1 for a in open_alarms if a.severity == Sev.critical
        )
        alarms_warning = sum(
            1 for a in open_alarms if a.severity == Sev.warning
        )
        alarms_info = sum(
            1 for a in open_alarms if a.severity == Sev.info
        )

        # Top 5 critical alarms
        critical_alarms = [
            {
                "id": str(a.id),
                "device_id": str(a.device_id) if a.device_id else None,
                "severity": a.severity.value if a.severity else "unknown",
                "message": a.notes or "",
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in sorted(
                open_alarms,
                key=lambda a: (
                    0 if a.severity == Sev.critical else 1,
                    a.created_at or datetime.min.replace(tzinfo=UTC),
                ),
                reverse=True,
            )[:5]
        ]

        return {
            "online_count": online_count,
            "total_count": total_count,
            "degraded_count": degraded_count,
            "offline_count": offline_count,
            "maintenance_count": maintenance_count,
            "uptime_percentage": uptime_percentage,
            "device_status_chart": device_status_chart,
            "alarms_critical": alarms_critical,
            "alarms_major": 0,
            "alarms_minor": alarms_info,
            "alarms_warning": alarms_warning,
            "active_alarms": critical_alarms,
        }


    @staticmethod
    def get_monitoring_dashboard_stats(
        db: Session,
        *,
        format_duration,
        format_bps,
    ) -> dict:
        """Build full monitoring dashboard data.

        Extends the base dashboard stats with performance metrics,
        device health table, subscriber count, alarms, and recent events.
        """
        from sqlalchemy import and_ as sa_and
        from sqlalchemy import func as sa_func

        from app.models.network_monitoring import DeviceStatus as DStatus
        from app.models.network_monitoring import MetricType as MT
        from app.models.usage import AccountingStatus, RadiusAccountingSession

        # ---- Device counts (reuse base logic inline) ----
        devices = (
            db.query(NetworkDevice)
            .filter(NetworkDevice.is_active.is_(True))
            .order_by(NetworkDevice.name)
            .all()
        )
        online_statuses = {DStatus.online, DStatus.degraded, DStatus.maintenance}
        devices_online = sum(1 for d in devices if d.status in online_statuses)
        devices_offline = sum(1 for d in devices if d.status == DStatus.offline)

        total_count = len(devices)
        online_count = sum(1 for d in devices if d.status == DStatus.online)
        degraded_count = sum(1 for d in devices if d.status == DStatus.degraded)
        offline_count = devices_offline
        maintenance_count = sum(1 for d in devices if d.status == DStatus.maintenance)

        device_status_chart = {
            "labels": ["Online", "Degraded", "Offline", "Maintenance"],
            "values": [online_count, degraded_count, offline_count, maintenance_count],
            "colors": ["#10b981", "#f59e0b", "#f43f5e", "#94a3b8"],
        }

        # ---- Alarms ----
        alarms = (
            db.query(Alert)
            .filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
            .order_by(Alert.triggered_at.desc())
            .limit(10)
            .all()
        )
        alarms_open = sum(1 for a in alarms if a.status == AlertStatus.open)

        # ---- Recent events ----
        recent_events = (
            db.query(AlertEvent)
            .order_by(AlertEvent.created_at.desc())
            .limit(10)
            .all()
        )

        # ---- Subscribers online ----
        subscribers_online = (
            db.query(sa_func.count(sa_func.distinct(RadiusAccountingSession.subscription_id)))
            .filter(RadiusAccountingSession.session_end.is_(None))
            .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
            .scalar()
            or 0
        )

        # ---- Performance metrics (latest per device) ----
        metric_types = [MT.cpu, MT.memory, MT.uptime, MT.rx_bps, MT.tx_bps]
        latest_subq = (
            db.query(
                DeviceMetric.device_id,
                DeviceMetric.metric_type,
                sa_func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .filter(DeviceMetric.metric_type.in_(metric_types))
            .group_by(DeviceMetric.device_id, DeviceMetric.metric_type)
            .subquery()
        )
        latest_metrics = (
            db.query(DeviceMetric)
            .join(
                latest_subq,
                sa_and(
                    DeviceMetric.device_id == latest_subq.c.device_id,
                    DeviceMetric.metric_type == latest_subq.c.metric_type,
                    DeviceMetric.recorded_at == latest_subq.c.latest,
                ),
            )
            .all()
        )

        metrics_by_device: dict[str, dict] = {}
        rx_total = 0.0
        tx_total = 0.0
        cpu_values: list[float] = []
        mem_values: list[float] = []
        for metric in latest_metrics:
            device_metrics = metrics_by_device.setdefault(str(metric.device_id), {})
            device_metrics[metric.metric_type] = metric
            if metric.metric_type == MT.rx_bps:
                rx_total += float(metric.value or 0)
            elif metric.metric_type == MT.tx_bps:
                tx_total += float(metric.value or 0)
            elif metric.metric_type == MT.cpu:
                cpu_values.append(float(metric.value or 0))
            elif metric.metric_type == MT.memory:
                mem_values.append(float(metric.value or 0))

        avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else None
        avg_mem = sum(mem_values) / len(mem_values) if mem_values else None

        # ---- Device health table ----
        device_health = []
        for device in devices:
            dm = metrics_by_device.get(str(device.id), {})
            cpu_m = dm.get(MT.cpu)
            mem_m = dm.get(MT.memory)
            uptime_m = dm.get(MT.uptime)
            device_health.append(
                {
                    "name": device.name,
                    "status": device.status.value if device.status else "unknown",
                    "health_status": device.health_status.value if device.health_status else "unknown",
                    "max_concurrent_subscribers": device.max_concurrent_subscribers,
                    "current_subscriber_count": device.current_subscriber_count or 0,
                    "cpu": f"{cpu_m.value:.1f}%" if cpu_m else "--",
                    "memory": f"{mem_m.value:.1f}%" if mem_m else "--",
                    "uptime": format_duration(uptime_m.value if uptime_m else None),
                    "last_seen": device.last_ping_at or device.last_snmp_at,
                }
            )

        return {
            "stats": {
                "devices_online": devices_online,
                "devices_offline": devices_offline,
                "alarms_open": alarms_open,
                "subscribers_online": subscribers_online,
            },
            "alarms": alarms,
            "recent_events": recent_events,
            "performance": {
                "avg_cpu": f"{avg_cpu:.1f}%" if avg_cpu is not None else "--",
                "avg_memory": f"{avg_mem:.1f}%" if avg_mem is not None else "--",
                "rx_bps": format_bps(rx_total) if rx_total > 0 else "--",
                "tx_bps": format_bps(tx_total) if tx_total > 0 else "--",
            },
            "device_health": device_health,
            "device_status_chart": device_status_chart,
        }


class DeviceInterfaces(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DeviceInterfaceCreate):
        interface = DeviceInterface(**payload.model_dump())
        db.add(interface)
        db.commit()
        db.refresh(interface)
        return interface

    @staticmethod
    def get(db: Session, interface_id: str):
        interface = db.get(DeviceInterface, interface_id)
        if not interface:
            raise HTTPException(status_code=404, detail="Device interface not found")
        return interface

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DeviceInterface)
        if device_id:
            query = query.filter(DeviceInterface.device_id == device_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": DeviceInterface.created_at, "name": DeviceInterface.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, interface_id: str, payload: DeviceInterfaceUpdate):
        interface = db.get(DeviceInterface, interface_id)
        if not interface:
            raise HTTPException(status_code=404, detail="Device interface not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(interface, key, value)
        db.commit()
        db.refresh(interface)
        return interface

    @staticmethod
    def delete(db: Session, interface_id: str):
        interface = db.get(DeviceInterface, interface_id)
        if not interface:
            raise HTTPException(status_code=404, detail="Device interface not found")
        db.delete(interface)
        db.commit()


class DeviceMetrics(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DeviceMetricCreate):
        metric = DeviceMetric(**payload.model_dump())
        db.add(metric)
        db.flush()
        _process_alerts(db, metric)
        db.commit()
        db.refresh(metric)
        return metric

    @staticmethod
    def get(db: Session, metric_id: str):
        metric = db.get(DeviceMetric, metric_id)
        if not metric:
            raise HTTPException(status_code=404, detail="Device metric not found")
        return metric

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        interface_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DeviceMetric)
        if device_id:
            query = query.filter(DeviceMetric.device_id == device_id)
        if interface_id:
            query = query.filter(DeviceMetric.interface_id == interface_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": DeviceMetric.created_at, "recorded_at": DeviceMetric.recorded_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, metric_id: str, payload: DeviceMetricUpdate):
        metric = db.get(DeviceMetric, metric_id)
        if not metric:
            raise HTTPException(status_code=404, detail="Device metric not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(metric, key, value)
        db.commit()
        db.refresh(metric)
        return metric

    @staticmethod
    def delete(db: Session, metric_id: str):
        metric = db.get(DeviceMetric, metric_id)
        if not metric:
            raise HTTPException(status_code=404, detail="Device metric not found")
        db.delete(metric)
        db.commit()


class AlertRules(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AlertRuleCreate):
        rule = AlertRule(**payload.model_dump())
        db.add(rule)
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def get(db: Session, rule_id: str):
        rule = db.get(AlertRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        return rule

    @staticmethod
    def list(
        db: Session,
        metric_type: str | None,
        device_id: str | None,
        interface_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AlertRule)
        if metric_type:
            query = query.filter(
                AlertRule.metric_type
                == validate_enum(metric_type, MetricType, "metric_type")
            )
        if device_id:
            query = query.filter(AlertRule.device_id == device_id)
        if interface_id:
            query = query.filter(AlertRule.interface_id == interface_id)
        if is_active is None:
            query = query.filter(AlertRule.is_active.is_(True))
        else:
            query = query.filter(AlertRule.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AlertRule.created_at, "name": AlertRule.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, rule_id: str, payload: AlertRuleUpdate):
        rule = db.get(AlertRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(rule, key, value)
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def delete(db: Session, rule_id: str):
        rule = db.get(AlertRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        rule.is_active = False
        db.commit()

    @staticmethod
    def bulk_update(db: Session, payload: AlertRuleBulkUpdateRequest) -> int:
        if not payload.rule_ids:
            raise HTTPException(status_code=400, detail="rule_ids required")
        ids = [coerce_uuid(rule_id) for rule_id in payload.rule_ids]
        rules = db.query(AlertRule).filter(AlertRule.id.in_(ids)).all()
        if len(rules) != len(ids):
            raise HTTPException(status_code=404, detail="One or more alert rules not found")
        for rule in rules:
            rule.is_active = payload.is_active
        db.commit()
        return len(rules)

    @staticmethod
    def bulk_update_response(db: Session, payload: AlertRuleBulkUpdateRequest) -> dict:
        updated = AlertRules.bulk_update(db, payload)
        return {"updated": updated}


class Alerts(ListResponseMixin):
    @staticmethod
    def get(db: Session, alert_id: str):
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        return alert

    @staticmethod
    def list(
        db: Session,
        rule_id: str | None,
        device_id: str | None,
        interface_id: str | None,
        status: str | None,
        severity: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Alert)
        if rule_id:
            query = query.filter(Alert.rule_id == rule_id)
        if device_id:
            query = query.filter(Alert.device_id == device_id)
        if interface_id:
            query = query.filter(Alert.interface_id == interface_id)
        if status:
            query = query.filter(
                Alert.status == validate_enum(status, AlertStatus, "status")
            )
        if severity:
            query = query.filter(
                Alert.severity
                == validate_enum(severity, AlertSeverity, "severity")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Alert.created_at, "triggered_at": Alert.triggered_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def acknowledge(db: Session, alert_id: str, payload: AlertAcknowledgeRequest):
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        alert.status = AlertStatus.acknowledged
        alert.acknowledged_at = datetime.now(UTC)
        event = AlertEvent(
            alert_id=alert.id,
            status=AlertStatus.acknowledged,
            message=payload.message or "Alert acknowledged",
        )
        db.add(event)
        db.commit()
        db.refresh(alert)
        return alert

    @staticmethod
    def resolve(db: Session, alert_id: str, payload: AlertResolveRequest):
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        alert.status = AlertStatus.resolved
        alert.resolved_at = datetime.now(UTC)
        event = AlertEvent(
            alert_id=alert.id,
            status=AlertStatus.resolved,
            message=payload.message or "Alert resolved",
        )
        db.add(event)
        db.commit()
        db.refresh(alert)
        return alert

    @staticmethod
    def bulk_acknowledge(
        db: Session, alert_ids: builtins.list[str], payload: AlertAcknowledgeRequest
    ) -> int:
        if not alert_ids:
            raise HTTPException(status_code=400, detail="alert_ids required")
        ids = [coerce_uuid(alert_id) for alert_id in alert_ids]
        alerts = db.query(Alert).filter(Alert.id.in_(ids)).all()
        if len(alerts) != len(ids):
            raise HTTPException(status_code=404, detail="One or more alerts not found")
        now = datetime.now(UTC)
        for alert in alerts:
            alert.status = AlertStatus.acknowledged
            alert.acknowledged_at = now
            event = AlertEvent(
                alert_id=alert.id,
                status=AlertStatus.acknowledged,
                message=payload.message or "Alert acknowledged",
            )
            db.add(event)
        db.commit()
        return len(alerts)

    @staticmethod
    def bulk_acknowledge_response(
        db: Session, alert_ids: builtins.list[str], payload: AlertAcknowledgeRequest
    ) -> dict[str, int]:
        updated = Alerts.bulk_acknowledge(db, alert_ids, payload)
        return {"updated": updated}

    @staticmethod
    def bulk_resolve(
        db: Session, alert_ids: builtins.list[str], payload: AlertResolveRequest
    ) -> int:
        if not alert_ids:
            raise HTTPException(status_code=400, detail="alert_ids required")
        ids = [coerce_uuid(alert_id) for alert_id in alert_ids]
        alerts = db.query(Alert).filter(Alert.id.in_(ids)).all()
        if len(alerts) != len(ids):
            raise HTTPException(status_code=404, detail="One or more alerts not found")
        now = datetime.now(UTC)
        for alert in alerts:
            alert.status = AlertStatus.resolved
            alert.resolved_at = now
            event = AlertEvent(
                alert_id=alert.id,
                status=AlertStatus.resolved,
                message=payload.message or "Alert resolved",
            )
            db.add(event)
        db.commit()
        return len(alerts)

    @staticmethod
    def bulk_resolve_response(
        db: Session, alert_ids: builtins.list[str], payload: AlertResolveRequest
    ) -> dict[str, int]:
        updated = Alerts.bulk_resolve(db, alert_ids, payload)
        return {"updated": updated}


class AlertEvents(ListResponseMixin):
    @staticmethod
    def list(
        db: Session,
        alert_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AlertEvent)
        if alert_id:
            query = query.filter(AlertEvent.alert_id == alert_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AlertEvent.created_at},
        )
        return apply_pagination(query, limit, offset).all()


def get_onu_status_summary(db: Session) -> dict[str, int]:
    """Aggregate ONT signal and online status for monitoring dashboard.

    Returns:
        Dictionary with total, online, offline, low_signal counts.
    """
    from sqlalchemy import func as sa_func

    from app.models.network import OntUnit, OnuOnlineStatus

    total = db.query(sa_func.count(OntUnit.id)).scalar() or 0
    online = (
        db.query(sa_func.count(OntUnit.id))
        .filter(OntUnit.online_status == OnuOnlineStatus.online)
        .scalar()
        or 0
    )
    offline = (
        db.query(sa_func.count(OntUnit.id))
        .filter(OntUnit.online_status == OnuOnlineStatus.offline)
        .scalar()
        or 0
    )

    # Low signal: ONTs with ONU Rx below warning threshold
    from app.services.network.olt_polling import get_signal_thresholds

    warn_threshold, _crit = get_signal_thresholds(db)
    low_signal = (
        db.query(sa_func.count(OntUnit.id))
        .filter(
            OntUnit.onu_rx_signal_dbm.isnot(None),
            OntUnit.onu_rx_signal_dbm < warn_threshold,
        )
        .scalar()
        or 0
    )

    return {
        "total": total,
        "online": online,
        "offline": offline,
        "low_signal": low_signal,
    }


def get_pon_outage_summary(db: Session) -> list[dict]:
    """Detect PON ports with multiple offline ONTs (possible fiber cut).

    Groups offline ONTs by PON port and returns ports exceeding the
    configured minimum offline threshold.

    Returns:
        List of dicts: {pon_port_name, olt_name, offline_count, total_count,
        offline_reasons, last_seen}.
    """
    from sqlalchemy import func as sa_func

    from app.models.network import (
        OLTDevice,
        OntAssignment,
        OntUnit,
        OnuOnlineStatus,
        PonPort,
    )

    # Load the minimum offline ONUs threshold from settings
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        raw = resolve_value(
            db, SettingDomain.network_monitoring, "pon_outage_min_offline_onus"
        )
        min_offline = int(str(raw)) if raw is not None else 2
    except Exception:
        min_offline = 2

    # Get offline ONTs with their assignments
    offline_onts = (
        db.query(
            OntUnit.id,
            OntUnit.offline_reason,
            OntUnit.last_seen_at,
            OntAssignment.pon_port_id,
        )
        .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
        .filter(
            OntUnit.online_status == OnuOnlineStatus.offline,
            OntAssignment.active.is_(True),
        )
        .all()
    )

    # Group by PON port
    port_offline: dict[str, list[dict]] = {}
    for ont_id, reason, last_seen, pon_port_id in offline_onts:
        port_key = str(pon_port_id) if pon_port_id else ""
        if not port_key:
            continue
        port_offline.setdefault(port_key, []).append(
            {"reason": reason.value if reason else "unknown", "last_seen": last_seen}
        )

    # Filter to ports exceeding threshold
    outage_port_ids = [
        pid for pid, items in port_offline.items() if len(items) >= min_offline
    ]
    if not outage_port_ids:
        return []

    # Enrich with PON port and OLT names + total ONT count per port
    pon_ports = (
        db.query(PonPort)
        .filter(PonPort.id.in_(outage_port_ids))
        .all()
    )
    pon_port_map = {str(p.id): p for p in pon_ports}

    # Get total assigned ONTs per port for context
    total_counts_raw = (
        db.query(
            OntAssignment.pon_port_id,
            sa_func.count(OntAssignment.id),
        )
        .filter(
            OntAssignment.pon_port_id.in_(outage_port_ids),
            OntAssignment.active.is_(True),
        )
        .group_by(OntAssignment.pon_port_id)
        .all()
    )
    total_per_port = {str(pid): cnt for pid, cnt in total_counts_raw}

    # Get OLT names
    olt_ids = list({str(p.olt_id) for p in pon_ports if p.olt_id})
    olts = db.query(OLTDevice).filter(OLTDevice.id.in_(olt_ids)).all() if olt_ids else []
    olt_map = {str(o.id): o.name for o in olts}

    results: list[dict] = []
    for port_id in outage_port_ids:
        port = pon_port_map.get(port_id)
        if not port:
            continue
        offline_items = port_offline[port_id]
        reasons: dict[str, int] = {}
        latest_seen = None
        for item in offline_items:
            r = item["reason"]
            reasons[r] = reasons.get(r, 0) + 1
            if item["last_seen"] and (latest_seen is None or item["last_seen"] > latest_seen):
                latest_seen = item["last_seen"]

        results.append(
            {
                "pon_port_name": port.name or str(port.id)[:8],
                "olt_name": olt_map.get(str(port.olt_id), "Unknown"),
                "offline_count": len(offline_items),
                "total_count": total_per_port.get(port_id, 0),
                "offline_reasons": reasons,
                "last_seen": latest_seen,
            }
        )

    # Sort by offline count descending
    results.sort(key=lambda x: x["offline_count"], reverse=True)
    return results


pop_sites = PopSites()
network_devices = NetworkDevices()
device_interfaces = DeviceInterfaces()
device_metrics = DeviceMetrics()
alert_rules = AlertRules()
alerts = Alerts()
alert_events = AlertEvents()
