"""Web service helpers for network operation history display."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.models.network_operation import (
    NetworkOperationTargetType,
)
from app.services.network_operations import network_operations

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

OPERATION_DISPLAY: dict[str, str] = {
    "olt_ont_sync": "OLT ONT Discovery",
    "olt_pon_repair": "PON Port Repair",
    "ont_provision": "ONT Provision",
    "ont_authorize": "ONT Authorize",
    "ont_reboot": "ONT Reboot",
    "ont_factory_reset": "ONT Factory Reset",
    "ont_set_pppoe": "Set PPPoE Credentials",
    "ont_set_conn_request_creds": "Set Connection Request Credentials",
    "ont_send_conn_request": "Connection Request",
    "ont_enable_ipv6": "Enable IPv6",
    "cpe_set_conn_request_creds": "Set Connection Request Credentials",
    "cpe_send_conn_request": "Connection Request",
    "cpe_reboot": "CPE Reboot",
    "cpe_factory_reset": "CPE Factory Reset",
    "tr069_bootstrap": "TR-069 Bootstrap",
    "wifi_update": "Wi-Fi Configuration",
    "pppoe_push": "PPPoE Push",
}


def _operation_title(op: Any) -> str:
    op_type_val = op.operation_type.value if op.operation_type else ""
    payload = getattr(op, "output_payload", None) or {}
    if (
        op_type_val == "olt_ont_sync"
        and isinstance(payload, dict)
        and payload.get("mode") == "pon_port_repair"
    ):
        return "PON Port Repair"
    return OPERATION_DISPLAY.get(op_type_val, op_type_val)


STATUS_CLASSES: dict[str, str] = {
    "pending": "bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200",
    "running": "bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200",
    "waiting": "bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200",
    "succeeded": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200",
    "failed": "bg-rose-100 text-rose-800 dark:bg-rose-900/60 dark:text-rose-200",
    "canceled": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

STATUS_DISPLAY: dict[str, str] = {
    "pending": "Pending",
    "running": "Running",
    "waiting": "Waiting",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "canceled": "Canceled",
}


def _format_duration(op: Any) -> str | None:
    """Format the duration between started_at and completed_at."""
    if not op.started_at or not op.completed_at:
        return None
    delta = op.completed_at - op.started_at
    total_seconds = int(delta.total_seconds())
    if total_seconds < 1:
        return "<1s"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def build_operation_history(
    db: Session,
    target_type: str,
    target_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Build formatted operation history for a device.

    Args:
        db: Database session.
        target_type: Device type string ("olt", "ont", "cpe").
        target_id: Device UUID string.
        limit: Maximum number of records.

    Returns:
        List of dicts ready for template rendering.
    """
    try:
        target_enum = NetworkOperationTargetType(target_type)
    except ValueError:
        logger.warning("Unknown target_type %r for operation history", target_type)
        return []
    ops = network_operations.list_for_device(db, target_enum, target_id, limit=limit)

    result: list[dict[str, Any]] = []
    for op in ops:
        status_val = op.status.value if op.status else "pending"
        op_type_val = op.operation_type.value if op.operation_type else ""

        entry: dict[str, Any] = {
            "id": str(op.id),
            "title": _operation_title(op),
            "status": STATUS_DISPLAY.get(status_val, status_val),
            "status_value": status_val,
            "status_class": STATUS_CLASSES.get(status_val, STATUS_CLASSES["pending"]),
            "is_running": status_val in ("running", "pending"),
            "is_waiting": status_val == "waiting",
            "is_failed": status_val == "failed",
            "message": op.error or op.waiting_reason or "",
            "initiated_by": op.initiated_by or "",
            "occurred_at": op.created_at,
            "duration": _format_duration(op),
            "can_retry": status_val == "failed",
            "operation_type": op_type_val,
            "target_type": target_type,
            "target_id": target_id,
        }
        result.append(entry)

    return result
