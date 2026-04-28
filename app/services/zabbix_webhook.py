"""Persistence helpers for Zabbix webhook routes."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.models.network import OLTDevice
from app.models.network_monitoring import (
    Alert,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    MetricType,
)


def find_device_by_zabbix_host_id(
    db: Session, zabbix_host_id: str
) -> tuple[str | None, UUID | None]:
    olt = db.scalars(
        select(OLTDevice).where(OLTDevice.zabbix_host_id == zabbix_host_id)
    ).first()
    if olt:
        return ("olt", olt.id)

    nas = db.scalars(
        select(NasDevice).where(NasDevice.zabbix_host_id == zabbix_host_id)
    ).first()
    if nas:
        return ("nas", nas.id)

    return (None, None)


def get_or_create_zabbix_alert_rule(db: Session) -> AlertRule:
    rule = db.scalars(
        select(AlertRule).where(AlertRule.name == "Zabbix Alert")
    ).first()
    if rule:
        return rule

    rule = AlertRule(
        name="Zabbix Alert",
        notes="Alerts forwarded from Zabbix monitoring",
        metric_type=MetricType.custom,
        severity=AlertSeverity.warning,
        threshold=0,
        operator="gt",
        is_active=True,
    )
    db.add(rule)
    db.flush()
    return rule


def find_open_zabbix_alert(
    db: Session,
    *,
    rule_id: UUID,
    zabbix_event_key: str,
) -> Alert | None:
    return db.scalars(
        select(Alert)
        .where(
            Alert.rule_id == rule_id,
            Alert.notes.contains(zabbix_event_key),
            Alert.status != AlertStatus.resolved,
        )
        .order_by(Alert.triggered_at.desc())
    ).first()
