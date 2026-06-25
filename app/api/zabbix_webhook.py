"""Zabbix webhook receiver for processing alerts and events.

This module receives webhooks from Zabbix actions and converts them
into internal notifications and alerts.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network_monitoring import (
    Alert,
    AlertEvent,
    AlertSeverity,
    AlertStatus,
    MetricType,
)
from app.services.zabbix import get_zabbix_webhook_token
from app.services.zabbix_webhook import (
    find_network_device_id_by_zabbix_host_id,
    find_open_zabbix_alert,
    get_or_create_zabbix_alert_rule,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/zabbix", tags=["zabbix-webhook"])

# Zabbix webhook authentication token (should match Zabbix action config)
WEBHOOK_TOKEN_HEADER = "X-Zabbix-Token"


def _require_zabbix_webhook_token(presented: str | None) -> None:
    """Fail closed unless the request presents the configured shared secret.

    These endpoints mount with no router-level auth and mutate state (alert
    records, device sync), so they must authenticate themselves. The token is
    resolved from ``ZABBIX_WEBHOOK_TOKEN`` (file/env/OpenBao). If it is not
    configured we reject with 503 rather than silently accepting anonymous
    callers; a configured-but-mismatched token is a 401. Compared in constant
    time to avoid leaking the secret via timing.
    """
    expected = get_zabbix_webhook_token()
    if not expected:
        logger.error(
            "zabbix_webhook_token_not_configured",
            extra={"event": "zabbix_webhook_token_not_configured"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Zabbix webhook authentication is not configured.",
        )
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Zabbix webhook token.",
        )


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

    # Tags (optional). Zabbix's default action template sends these as a LIST of
    # {"tag": ..., "value": ...} objects; older/custom templates may send a flat
    # {tag: value} dict. Normalized to a dict by the validator below.
    tags: dict[str, str] | None = None

    # Acknowledge info (for OK/resolved)
    ack_message: str | None = Field(default=None, alias="ackMessage")
    ack_user: str | None = Field(default=None, alias="ackUser")

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> dict[str, str] | None:
        """Accept Zabbix's native list form and flatten to {tag: value}.

        Zabbix posts `tags` as `[{"tag": "scope", "value": "availability"}, ...]`.
        Without this, a real alert is rejected 422 on an unused field. Duplicate
        tag keys keep the last value; a dict input passes through unchanged.
        """
        if isinstance(value, list):
            return {
                str(item["tag"]): str(item.get("value", ""))
                for item in value
                if isinstance(item, dict) and "tag" in item
            }
        return value


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


def _parse_item_value(item_value: str | None) -> float:
    """Parse an item value to float, returning 0.0 on failure.

    ``measured_value`` is a non-nullable column, so an unparseable value still
    falls back to 0.0 — but the raw string is preserved verbatim in ``notes``
    and a parse failure is logged, so a real-but-unparseable value is no longer
    silently indistinguishable from a true zero.
    """
    if not item_value:
        return 0.0
    value = item_value.strip()
    multipliers = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12, "p": 1e15, "e": 1e18}
    try:
        if value and value[-1].lower() in multipliers:
            return float(value[:-1]) * multipliers[value[-1].lower()]
        return float(value)
    except (ValueError, TypeError):
        logger.info(
            "zabbix_item_value_unparseable",
            extra={"event": "zabbix_item_value_unparseable", "raw": value[:64]},
        )
        return 0.0


def _event_triggered_at(payload: ZabbixAlertPayload) -> datetime:
    """Best-effort Zabbix event time for ``triggered_at``.

    Zabbix sends ``eventDate`` (``yyyy.mm.dd``) + ``eventTime`` (``hh:mm:ss``).
    We parse them so the recorded trigger time reflects when Zabbix saw the
    problem rather than when we received the webhook (which drifts under retry/
    backlog). Zabbix omits a timezone; we assume the server runs UTC (the common
    case) and fall back to receipt time if the fields are absent/malformed.
    """
    if payload.event_date and payload.event_time:
        try:
            return datetime.strptime(
                f"{payload.event_date} {payload.event_time}", "%Y.%m.%d %H:%M:%S"
            ).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            logger.info(
                "zabbix_event_time_unparseable",
                extra={
                    "event": "zabbix_event_time_unparseable",
                    "date": payload.event_date,
                    "time": payload.event_time,
                },
            )
    return datetime.now(UTC)


def _response(
    *,
    alert_id: str | None = None,
    message: str | None = None,
) -> ZabbixWebhookResponse:
    return ZabbixWebhookResponse(
        status="ok",
        alert_id=alert_id,
        message=message,
    )


@router.post("/webhook/alert", response_model=ZabbixWebhookResponse)
async def receive_zabbix_alert(
    request: Request,
    db: Session = Depends(get_db),
    x_zabbix_token: str | None = Header(default=None, alias="X-Zabbix-Token"),
):
    """Receive alert webhook from Zabbix.

    This endpoint converts Zabbix trigger notifications into internal
    Alert records, enabling unified alert management across all monitoring
    sources.

    Authentication is enforced *before* the body is parsed: unauthenticated
    callers (scanners, misconfigured senders) get 401 and never reach
    validation, so they neither generate 422 noise nor learn the payload
    schema. On a malformed authenticated payload we log the raw body to help
    reconcile the Zabbix action template against ``ZabbixAlertPayload``, then
    return 422.
    """
    _require_zabbix_webhook_token(x_zabbix_token)

    raw_body = await request.body()
    try:
        data = json.loads(raw_body) if raw_body else {}
        payload = ZabbixAlertPayload.model_validate(data)
    except (ValueError, ValidationError) as exc:
        logger.warning(
            "zabbix_webhook_invalid_payload",
            extra={
                "event": "zabbix_webhook_invalid_payload",
                "content_type": request.headers.get("content-type"),
                "raw_body": raw_body.decode("utf-8", "replace")[:2000],
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid Zabbix alert payload",
        ) from exc

    # SQLAlchemy work is synchronous; run it off the event loop so a slow DB
    # call cannot stall this single-worker process.
    return await run_in_threadpool(_persist_zabbix_alert, db, payload)


def _persist_zabbix_alert(
    db: Session, payload: ZabbixAlertPayload
) -> ZabbixWebhookResponse:
    """Classify + write the alert, then commit (or roll back on failure).

    The request session (``get_db``) does not commit on its own, so without this
    the flushed Alert/AlertEvent rows would be discarded when the session
    closes — the table would stay empty in production.
    """
    try:
        response = _write_zabbix_alert(db, payload)
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


def _emit_network_alert(payload: ZabbixAlertPayload, *, resolved: bool) -> None:
    """Surface a Zabbix trigger as a network_alert event (webhook fan-out).

    Without this the alert only becomes a DB row that nothing reads. Emitting
    routes it through the same event pipeline as other network alerts. Uses its
    own short-lived session (like the auth-failure alert) so it can never poison
    the alert transaction, and swallows failures — the Alert row is the source
    of truth and must persist regardless.
    """
    try:
        from app.services.db_session_adapter import db_session_adapter
        from app.services.events import emit_event
        from app.services.events.types import EventType

        db = db_session_adapter.create_session()
        try:
            emit_event(
                db,
                EventType.network_alert,
                {
                    "alert_type": "zabbix_trigger",
                    "integration": "zabbix",
                    "status": "resolved" if resolved else "problem",
                    "zabbix_severity": payload.trigger_severity,
                    "trigger_id": payload.trigger_id,
                    "trigger_name": payload.trigger_name,
                    "host_id": payload.host_id,
                    "host_name": payload.host_name,
                    "item_name": payload.item_name,
                    "item_value": payload.item_value,
                },
                actor="zabbix",
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("zabbix_alert_event_emit_failed")


def _write_zabbix_alert(
    db: Session, payload: ZabbixAlertPayload
) -> ZabbixWebhookResponse:
    """Convert a validated Zabbix alert into Alert/AlertEvent records."""
    logger.info(
        "zabbix_webhook_received",
        extra={
            "trigger_id": payload.trigger_id,
            "host_id": payload.host_id,
            "status": payload.trigger_status,
        },
    )

    # Get or create Zabbix alert rule
    rule = get_or_create_zabbix_alert_rule(db)

    # Map severity
    severity = _map_zabbix_severity(payload.trigger_severity)

    # Parse item value if available
    measured_value = _parse_item_value(payload.item_value)

    # Determine if this is a new alert, update, or resolution. Prefer the
    # authoritative Zabbix event_value macro (1=PROBLEM, 0=OK/recovery); fall
    # back to the free-text trigger_status only when it's absent. Keying off the
    # status string alone would treat a suppressed/localized status (anything
    # not exactly "PROBLEM") as a recovery and wrongly resolve a live alert.
    if payload.event_value is not None and payload.event_value != "":
        is_problem = payload.event_value == "1"
    else:
        is_problem = payload.trigger_status.upper() == "PROBLEM"

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
                device_id=find_network_device_id_by_zabbix_host_id(db, payload.host_id),
                metric_type=MetricType.custom,
                measured_value=measured_value,
                severity=severity,
                status=AlertStatus.open,
                triggered_at=_event_triggered_at(payload),
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
            _emit_network_alert(payload, resolved=False)

            return _response(
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
            _emit_network_alert(payload, resolved=True)

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
    _require_zabbix_webhook_token(x_zabbix_token)
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
