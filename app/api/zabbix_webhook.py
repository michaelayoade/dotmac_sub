"""Zabbix webhook receiver for processing alerts and events.

This module receives webhooks from Zabbix actions and converts them
into internal notifications and alerts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.catalog import NasDevice
from app.models.network import OLTDevice
from app.models.network_monitoring import (
    Alert,
    AlertEvent,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    MetricType,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/zabbix", tags=["zabbix-webhook"])

# Zabbix webhook authentication token (should match Zabbix action config)
WEBHOOK_TOKEN_HEADER = "X-Zabbix-Token"


class ZabbixAlertStatus(str, Enum):
    """Zabbix trigger status values."""

    problem = "PROBLEM"
    ok = "OK"
    resolved = "RESOLVED"


class ZabbixSeverity(str, Enum):
    """Zabbix trigger severity levels."""

    not_classified = "Not classified"
    information = "Information"
    warning = "Warning"
    average = "Average"
    high = "High"
    disaster = "Disaster"


class ZabbixAlertPayload(BaseModel):
    """Payload received from Zabbix webhook action."""

    model_config = ConfigDict(extra="allow")

    # Zabbix trigger info
    trigger_id: str = Field(alias="triggerId")
    trigger_name: str = Field(alias="triggerName")
    trigger_status: str = Field(alias="triggerStatus")
    trigger_severity: str = Field(alias="triggerSeverity")
    trigger_url: str | None = Field(default=None, alias="triggerUrl")

    # Zabbix host info
    host_id: str = Field(alias="hostId")
    host_name: str = Field(alias="hostName")
    host_ip: str | None = Field(default=None, alias="hostIp")

    # Event info
    event_id: str = Field(alias="eventId")
    event_time: str | None = Field(default=None, alias="eventTime")
    event_date: str | None = Field(default=None, alias="eventDate")
    event_value: str | None = Field(default=None, alias="eventValue")

    # Item info (optional)
    item_id: str | None = Field(default=None, alias="itemId")
    item_name: str | None = Field(default=None, alias="itemName")
    item_value: str | None = Field(default=None, alias="itemValue")
    item_key: str | None = Field(default=None, alias="itemKey")

    # Tags (optional, passed as JSON string or dict)
    tags: dict[str, str] | None = None

    # Acknowledge info (for OK/resolved)
    ack_message: str | None = Field(default=None, alias="ackMessage")
    ack_user: str | None = Field(default=None, alias="ackUser")


class ZabbixWebhookResponse(BaseModel):
    """Response for Zabbix webhook."""

    status: str = "ok"
    alert_id: str | None = None
    message: str | None = None


def _map_zabbix_severity(zabbix_severity: str) -> AlertSeverity:
    """Map Zabbix severity to internal AlertSeverity."""
    severity_map = {
        "not classified": AlertSeverity.info,
        "information": AlertSeverity.info,
        "warning": AlertSeverity.warning,
        "average": AlertSeverity.warning,
        "high": AlertSeverity.critical,
        "disaster": AlertSeverity.critical,
    }
    return severity_map.get(zabbix_severity.lower(), AlertSeverity.warning)


def _find_device_by_zabbix_host_id(
    db: Session, zabbix_host_id: str
) -> tuple[str | None, UUID | None]:
    """Find OLT or NAS device by Zabbix host ID.

    Returns (device_type, device_id) or (None, None) if not found.
    """
    # Check OLTs first
    olt = db.scalars(
        select(OLTDevice).where(OLTDevice.zabbix_host_id == zabbix_host_id)
    ).first()
    if olt:
        return ("olt", olt.id)

    # Check NAS devices
    nas = db.scalars(
        select(NasDevice).where(NasDevice.zabbix_host_id == zabbix_host_id)
    ).first()
    if nas:
        return ("nas", nas.id)

    return (None, None)


def _get_or_create_zabbix_alert_rule(db: Session) -> AlertRule:
    """Get or create a default alert rule for Zabbix-sourced alerts."""
    rule = db.scalars(
        select(AlertRule).where(AlertRule.name == "Zabbix Alert")
    ).first()

    if not rule:
        rule = AlertRule(
            name="Zabbix Alert",
            notes="Alerts forwarded from Zabbix monitoring",
            metric_type=MetricType.custom,
            severity=AlertSeverity.warning,
            threshold=0,
            operator="gt",  # gt=greater than, gte=>=, lt=<, lte=<=, eq==
            is_active=True,
        )
        db.add(rule)
        db.flush()

    return rule


def _parse_item_value(item_value: str | None) -> float:
    """Parse item value to float, returning 0.0 on failure."""
    if not item_value:
        return 0.0
    try:
        # Handle common suffixes (K, M, G, etc.)
        value = item_value.strip()
        multipliers = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}
        if value and value[-1].lower() in multipliers:
            return float(value[:-1]) * multipliers[value[-1].lower()]
        return float(value)
    except (ValueError, TypeError):
        return 0.0


@router.post("/webhook/alert", response_model=ZabbixWebhookResponse)
def receive_zabbix_alert(
    payload: ZabbixAlertPayload,
    db: Session = Depends(get_db),
    x_zabbix_token: str | None = Header(default=None, alias="X-Zabbix-Token"),
):
    """Receive alert webhook from Zabbix.

    This endpoint converts Zabbix trigger notifications into internal
    Alert records, enabling unified alert management across all monitoring
    sources.
    """
    logger.info(
        "zabbix_webhook_received",
        extra={
            "trigger_id": payload.trigger_id,
            "host_id": payload.host_id,
            "status": payload.trigger_status,
        },
    )

    # Find associated device
    device_type, device_id = _find_device_by_zabbix_host_id(db, payload.host_id)

    # Get or create Zabbix alert rule
    rule = _get_or_create_zabbix_alert_rule(db)

    # Map severity
    severity = _map_zabbix_severity(payload.trigger_severity)

    # Parse item value if available
    measured_value = _parse_item_value(payload.item_value)

    # Determine if this is a new alert, update, or resolution
    is_problem = payload.trigger_status.upper() == "PROBLEM"

    # Build unique identifier for deduplication
    zabbix_event_key = f"zabbix:{payload.trigger_id}:{payload.host_id}"

    # Check for existing alert
    existing_alert = db.scalars(
        select(Alert)
        .where(
            Alert.rule_id == rule.id,
            Alert.notes.contains(zabbix_event_key),
            Alert.status != AlertStatus.resolved,
        )
        .order_by(Alert.triggered_at.desc())
    ).first()

    if is_problem:
        if existing_alert:
            # Update existing alert
            existing_alert.severity = severity
            existing_alert.measured_value = measured_value

            # Add event
            event = AlertEvent(
                alert_id=existing_alert.id,
                status=AlertStatus.open,
                message=f"Updated by Zabbix event {payload.event_id}"[:255],
            )
            db.add(event)
            db.flush()

            logger.info(
                "zabbix_alert_updated",
                extra={"alert_id": str(existing_alert.id)},
            )

            return ZabbixWebhookResponse(
                status="ok",
                alert_id=str(existing_alert.id),
                message="Alert updated",
            )
        else:
            # Create new alert
            notes = (
                f"{zabbix_event_key}\n"
                f"Trigger: {payload.trigger_name}\n"
                f"Host: {payload.host_name}"
            )
            if payload.item_name:
                notes += f"\nItem: {payload.item_name}"
            if payload.item_value:
                notes += f"\nValue: {payload.item_value}"

            alert = Alert(
                rule_id=rule.id,
                metric_type=MetricType.custom,
                measured_value=measured_value,
                severity=severity,
                status=AlertStatus.open,
                triggered_at=datetime.now(UTC),
                notes=notes,
            )
            db.add(alert)
            db.flush()

            # Add triggered event
            event = AlertEvent(
                alert_id=alert.id,
                status=AlertStatus.open,
                message=f"Created from Zabbix event {payload.event_id}"[:255],
            )
            db.add(event)
            db.flush()

            logger.info(
                "zabbix_alert_created",
                extra={"alert_id": str(alert.id)},
            )

            return ZabbixWebhookResponse(
                status="ok",
                alert_id=str(alert.id),
                message="Alert created",
            )
    else:
        # This is a recovery (OK/RESOLVED)
        if existing_alert:
            existing_alert.status = AlertStatus.resolved
            existing_alert.resolved_at = datetime.now(UTC)

            # Add resolved event
            event = AlertEvent(
                alert_id=existing_alert.id,
                status=AlertStatus.resolved,
                message=f"Resolved by Zabbix event {payload.event_id}"[:255],
            )
            db.add(event)
            db.flush()

            logger.info(
                "zabbix_alert_resolved",
                extra={"alert_id": str(existing_alert.id)},
            )

            return ZabbixWebhookResponse(
                status="ok",
                alert_id=str(existing_alert.id),
                message="Alert resolved",
            )
        else:
            logger.info(
                "zabbix_alert_resolve_no_match",
                extra={"trigger_id": payload.trigger_id},
            )
            return ZabbixWebhookResponse(
                status="ok",
                message="No matching alert to resolve",
            )


@router.post("/webhook/sync", response_model=dict[str, Any])
def trigger_device_sync(
    db: Session = Depends(get_db),
    x_zabbix_token: str | None = Header(default=None, alias="X-Zabbix-Token"),
):
    """Manually trigger device sync to Zabbix.

    This endpoint can be called to sync all DotMac devices to Zabbix hosts.
    Useful for initial setup or after bulk device changes.
    """
    from app.services.zabbix_host_sync import sync_all_devices

    try:
        result = sync_all_devices(db)
        db.commit()
        return {"status": "ok", "result": result}
    except Exception as exc:
        logger.exception("zabbix_sync_failed")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {exc}",
        ) from exc
