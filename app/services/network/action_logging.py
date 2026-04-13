"""Central logging helpers for OLT and ONT operator actions."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.services import web_admin as web_admin_service

logger = logging.getLogger(__name__)


def actor_label(request: Any | None) -> str:
    """Return a stable actor label from the admin request context."""
    if request is None:
        return "unknown"
    try:
        current_user = web_admin_service.get_current_user(request)
    except Exception:
        return "unknown"
    if not isinstance(current_user, Mapping):
        return "unknown"
    return str(
        current_user.get("name")
        or current_user.get("email")
        or current_user.get("actor_id")
        or current_user.get("subscriber_id")
        or "unknown"
    )


def looks_like_prerequisite_failure(message: str) -> bool:
    """Classify operator failures caused by missing setup or stale prerequisites."""
    text = str(message or "").lower()
    markers = [
        "not found",
        "no profile selected",
        "no firmware image selected",
        "missing",
        "required",
        "not configured",
        "incomplete",
        "not linked",
        "not managed",
        "not bootstrapped",
        "waiting for",
        "cannot determine",
        "resolution failed",
        "no associated olt",
        "no active assignment",
        "olt context is incomplete",
        "no tr-069 device",
        "no matching genieacs device",
        "no live acs",
        "no connectionrequesturl",
        "connection request url",
        "no ssh",
        "ssh credentials",
        "snmp credentials",
        "no snmp",
        "no response from device",
        "connection refused",
        "timed out",
        "timeout",
        "unreachable",
        "authentication failed",
        "permission denied",
        "missing port or serial number",
        "missing ont selection",
        "target profile",
        "linked device not found",
    ]
    return any(marker in text for marker in markers)


def log_blocked_network_action(
    *,
    request: Any | None,
    resource_type: str,
    resource_id: str | None,
    action: str,
    message: str,
    waiting: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Log operator actions blocked by missing OLT/ONT prerequisites."""
    if waiting or not looks_like_prerequisite_failure(message):
        return

    actor = actor_label(request)
    safe_metadata = dict(metadata or {})
    logger.error(
        "Network action blocked by missing prerequisite: resource=%s resource_id=%s action=%s actor=%s reason=%s",
        resource_type,
        resource_id or "unknown",
        action,
        actor,
        message,
        extra={
            "event": "network_action_prerequisite_blocked",
            "network_resource_type": resource_type,
            "network_resource_id": resource_id,
            "network_action": action,
            "actor": actor,
            "reason": message,
            "metadata": safe_metadata,
        },
    )


def log_network_action_result(
    *,
    request: Any | None,
    resource_type: str,
    resource_id: str | None,
    action: str,
    success: bool,
    message: str,
    waiting: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Log failed OLT/ONT action results when they are prerequisite failures."""
    if success:
        return
    log_blocked_network_action(
        request=request,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        message=message,
        waiting=waiting,
        metadata=metadata,
    )
