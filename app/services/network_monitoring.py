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


pop_sites = PopSites()
network_devices = NetworkDevices()
device_interfaces = DeviceInterfaces()
device_metrics = DeviceMetrics()
alert_rules = AlertRules()
alerts = Alerts()
alert_events = AlertEvents()
