"""Service helpers for admin network alarm-rule web routes."""

from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session, selectinload

from app.models.network_monitoring import (
    AlertOperator,
    AlertSeverity,
    DeviceInterface,
    MetricType,
    NetworkDevice,
)
from app.schemas.network_monitoring import AlertRuleCreate
from app.services import network_monitoring as monitoring_service


class AlarmRuleFormValues(TypedDict):
    name: str
    metric_type: str
    operator: str
    threshold_raw: str
    duration_raw: str
    severity: str
    device_id_raw: str
    interface_id_raw: str
    notes: str | None
    is_active: bool


class AlarmRuleNormalized(AlarmRuleFormValues):
    threshold: float
    duration_seconds: int | None
    device_id: UUID | None
    interface_id: UUID | None


def form_options(db: Session) -> dict[str, object]:
    """Return shared select options for alarm-rule form."""
    devices = (
        db.query(NetworkDevice)
        .filter(NetworkDevice.is_active.is_(True))
        .order_by(NetworkDevice.name)
        .all()
    )
    interfaces = (
        db.query(DeviceInterface)
        .options(selectinload(DeviceInterface.device))
        .order_by(DeviceInterface.name)
        .all()
    )
    return {
        "devices": devices,
        "interfaces": interfaces,
        "metric_types": list(MetricType),
        "operators": list(AlertOperator),
        "severities": list(AlertSeverity),
    }


def parse_form_values(form) -> AlarmRuleFormValues:
    """Parse alarm-rule form fields into normalized values."""
    return {
        "name": (form.get("name") or "").strip(),
        "metric_type": form.get("metric_type") or MetricType.custom.value,
        "operator": form.get("operator") or AlertOperator.gt.value,
        "threshold_raw": (form.get("threshold") or "").strip(),
        "duration_raw": (form.get("duration_seconds") or "").strip(),
        "severity": form.get("severity") or AlertSeverity.warning.value,
        "device_id_raw": (form.get("device_id") or "").strip(),
        "interface_id_raw": (form.get("interface_id") or "").strip(),
        "notes": (form.get("notes") or "").strip() or None,
        "is_active": form.get("is_active") == "true",
    }


def validate_form_values(values: AlarmRuleFormValues) -> tuple[AlarmRuleNormalized | None, str | None]:
    """Validate/normalize alarm-rule form fields."""
    threshold_raw = values["threshold_raw"]
    duration_raw = values["duration_raw"]
    device_id_raw = values["device_id_raw"]
    interface_id_raw = values["interface_id_raw"]

    if not values["name"]:
        return None, "Rule name is required."

    try:
        threshold = float(threshold_raw)
    except ValueError:
        return None, "Threshold must be a number."

    duration_seconds = None
    if duration_raw:
        try:
            duration_seconds = int(duration_raw)
        except ValueError:
            return None, "Duration must be a whole number of seconds."

    device_id = None
    if device_id_raw:
        try:
            device_id = UUID(device_id_raw)
        except ValueError:
            return None, "Invalid device selection."

    interface_id = None
    if interface_id_raw:
        try:
            interface_id = UUID(interface_id_raw)
        except ValueError:
            return None, "Invalid interface selection."

    normalized: AlarmRuleNormalized = {
        **values,
        "threshold": threshold,
        "duration_seconds": duration_seconds,
        "device_id": device_id,
        "interface_id": interface_id,
    }
    return normalized, None


def create_rule(db: Session, normalized: AlarmRuleNormalized) -> str | None:
    """Create alarm rule, returning error message on failure."""
    try:
        payload = AlertRuleCreate(
            name=normalized["name"],
            metric_type=MetricType(str(normalized["metric_type"])),
            operator=AlertOperator(str(normalized["operator"])),
            threshold=normalized["threshold"],
            duration_seconds=normalized["duration_seconds"],
            severity=AlertSeverity(str(normalized["severity"])),
            device_id=normalized["device_id"],
            interface_id=normalized["interface_id"],
            is_active=normalized["is_active"],
            notes=normalized["notes"],
        )
        monitoring_service.alert_rules.create(db=db, payload=payload)
        return None
    except ValidationError as exc:
        return exc.errors()[0].get("msg") or "Please correct the highlighted fields."
    except Exception as exc:
        return str(exc)


def rule_form_data(values: AlarmRuleFormValues) -> dict[str, object]:
    """Build rule-like object for form re-render after errors."""
    return {
        "name": values.get("name"),
        "metric_type": values.get("metric_type"),
        "operator": values.get("operator"),
        "threshold": values.get("threshold_raw"),
        "duration_seconds": values.get("duration_raw"),
        "severity": values.get("severity"),
        "device_id": values.get("device_id_raw"),
        "interface_id": values.get("interface_id_raw"),
        "is_active": bool(values.get("is_active")),
        "notes": values.get("notes") or "",
    }
