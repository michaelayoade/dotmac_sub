"""Zabbix webhook receiver for processing alerts and events.

This module receives webhooks from Zabbix actions and converts them
into internal notifications and alerts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network_monitoring import (
    Alert,
    AlertEvent,
    AlertSeverity,
    AlertStatus,
    MetricType,
)
from app.services.autofind_trigger import (
    AutofindTriggerResult,
    trigger_autofind_by_identifier,
)
from app.services.zabbix_webhook import (
    find_device_by_zabbix_host_id,
    find_open_zabbix_alert,
    get_or_create_zabbix_alert_rule,
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
    autofind_triggered: bool = False
    autofind_task_id: str | None = None
    autofind_message: str | None = None


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


def _is_autofind_problem(payload: ZabbixAlertPayload) -> bool:
    """Return True when a Zabbix problem event should trigger OLT autofind."""
    if payload.trigger_status.upper() != "PROBLEM":
        return False

    tags = {str(k).lower(): str(v).lower() for k, v in (payload.tags or {}).items()}
    tag_values = set(tags.values())
    if (
        tags.get("dotmac_event") == "autofind"
        or tags.get("event_type") == "autofind"
        or tags.get("autofind") in {"1", "true", "yes"}
        or "autofind" in tag_values
    ):
        return True

    text = " ".join(
        part
        for part in (
            payload.trigger_name,
            payload.item_name or "",
            payload.item_key or "",
        )
        if part
    ).lower()
    markers = (
        "ontautofind",
        "ont autofind",
        "autofind",
        "unconfigured ont",
        "unauthorized ont",
        "unauthenticated ont",
        "rogue ont",
    )
    return any(marker in text for marker in markers)


def _trigger_autofind_for_zabbix_problem(
    db: Session,
    payload: ZabbixAlertPayload,
    *,
    device_type: str | None,
    device_id: Any | None,
) -> AutofindTriggerResult | None:
    if not _is_autofind_problem(payload):
        return None

    identifiers: list[str] = []
    if device_type == "olt" and device_id is not None:
        identifiers.append(str(device_id))
    if payload.host_ip:
        identifiers.append(payload.host_ip)
    identifiers.append(payload.host_name)

    seen: set[str] = set()
    for identifier in identifiers:
        normalized = str(identifier or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result = trigger_autofind_by_identifier(
            db=db,
            identifier=normalized,
            source="zabbix",
        )
        if result.triggered or result.olt_id:
            return result

    return AutofindTriggerResult(
        triggered=False,
        reason="No active OLT matched Zabbix autofind event",
    )


def _response(
    *,
    alert_id: str | None = None,
    message: str | None = None,
    autofind_result: AutofindTriggerResult | None = None,
) -> ZabbixWebhookResponse:
    return ZabbixWebhookResponse(
        status="ok",
        alert_id=alert_id,
        message=message,
        autofind_triggered=bool(autofind_result and autofind_result.triggered),
        autofind_task_id=autofind_result.task_id if autofind_result else None,
        autofind_message=autofind_result.reason if autofind_result else None,
    )


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
    device_type, device_id = find_device_by_zabbix_host_id(db, payload.host_id)

    # Get or create Zabbix alert rule
    rule = get_or_create_zabbix_alert_rule(db)

    # Map severity
    severity = _map_zabbix_severity(payload.trigger_severity)

    # Parse item value if available
    measured_value = _parse_item_value(payload.item_value)

    # Determine if this is a new alert, update, or resolution
    is_problem = payload.trigger_status.upper() == "PROBLEM"
    autofind_result = _trigger_autofind_for_zabbix_problem(
        db,
        payload,
        device_type=device_type,
        device_id=device_id,
    )
    if autofind_result:
        logger.info(
            "zabbix_autofind_trigger_result",
            extra={
                "triggered": autofind_result.triggered,
                "olt_id": autofind_result.olt_id,
                "task_id": autofind_result.task_id,
                "reason": autofind_result.reason,
                "host_id": payload.host_id,
            },
        )

    # Build unique identifier for deduplication
    zabbix_event_key = f"zabbix:{payload.trigger_id}:{payload.host_id}"

    # Check for existing alert
    existing_alert = find_open_zabbix_alert(
        db,
        rule_id=rule.id,
        zabbix_event_key=zabbix_event_key,
    )

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

            return _response(
                alert_id=str(existing_alert.id),
                message="Alert updated",
                autofind_result=autofind_result,
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

            return _response(
                alert_id=str(alert.id),
                message="Alert created",
                autofind_result=autofind_result,
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

            return _response(
                alert_id=str(existing_alert.id),
                message="Alert resolved",
            )
        else:
            logger.info(
                "zabbix_alert_resolve_no_match",
                extra={"trigger_id": payload.trigger_id},
            )
            return _response(message="No matching alert to resolve")


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
