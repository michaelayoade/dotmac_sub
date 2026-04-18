"""Service helpers for remote CPE action web routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.cpe_actions import ActionResult, CpeActions
from app.services.network_operations import run_tracked_action
from app.services.web_network_cpe_audit import (
    actor_name_from_request,
    log_cpe_audit_event,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def execute_reboot(
    db: Session, cpe_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Reboot CPE device with operation tracking."""
    return run_tracked_action(
        db,
        NetworkOperationType.cpe_reboot,
        NetworkOperationTargetType.cpe,
        cpe_id,
        lambda: CpeActions.reboot(db, cpe_id),
        correlation_key=f"cpe_reboot:{cpe_id}",
        initiated_by=initiated_by,
    )


def execute_reboot_from_request(db: Session, cpe_id: str, *, request) -> ActionResult:
    result = execute_reboot(db, cpe_id, initiated_by=actor_name_from_request(request))
    log_cpe_audit_event(
        db,
        request=request,
        action="reboot",
        entity_id=cpe_id,
        metadata={"success": result.success, "message": result.message},
        is_success=result.success,
    )
    return result


def execute_factory_reset(
    db: Session, cpe_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Factory reset CPE device with operation tracking."""
    return run_tracked_action(
        db,
        NetworkOperationType.cpe_factory_reset,
        NetworkOperationTargetType.cpe,
        cpe_id,
        lambda: CpeActions.factory_reset(db, cpe_id),
        correlation_key=f"cpe_factory_reset:{cpe_id}",
        initiated_by=initiated_by,
    )


def execute_factory_reset_from_request(
    db: Session, cpe_id: str, *, request
) -> ActionResult:
    result = execute_factory_reset(
        db, cpe_id, initiated_by=actor_name_from_request(request)
    )
    log_cpe_audit_event(
        db,
        request=request,
        action="factory_reset",
        entity_id=cpe_id,
        metadata={"success": result.success, "message": result.message},
        is_success=result.success,
    )
    return result


def execute_connection_request(
    db: Session, cpe_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Send connection request to CPE with operation tracking."""
    return run_tracked_action(
        db,
        NetworkOperationType.cpe_send_conn_request,
        NetworkOperationTargetType.cpe,
        cpe_id,
        lambda: CpeActions.send_connection_request(db, cpe_id),
        correlation_key=f"cpe_conn_req:{cpe_id}",
        initiated_by=initiated_by,
    )


def execute_connection_request_from_request(
    db: Session, cpe_id: str, *, request
) -> ActionResult:
    return execute_connection_request(
        db, cpe_id, initiated_by=actor_name_from_request(request)
    )


def execute_refresh_from_request(db: Session, cpe_id: str, *, request) -> ActionResult:
    result = CpeActions.refresh_status(db, cpe_id)
    log_cpe_audit_event(
        db,
        request=request,
        action="refresh",
        entity_id=cpe_id,
        metadata={"success": result.success},
        is_success=result.success,
    )
    return result


def execute_wifi_ssid(db: Session, cpe_id: str, *, ssid: str) -> ActionResult:
    return CpeActions.set_wifi_ssid(db, cpe_id, ssid)


def execute_wifi_password(db: Session, cpe_id: str, *, password: str) -> ActionResult:
    return CpeActions.set_wifi_password(db, cpe_id, password)


def execute_lan_port(
    db: Session, cpe_id: str, *, port: int, enabled: bool
) -> ActionResult:
    return CpeActions.toggle_lan_port(db, cpe_id, port, enabled)


def execute_ping_diagnostic(
    db: Session, cpe_id: str, *, host: str, count: int
) -> ActionResult:
    return CpeActions.run_ping_diagnostic(db, cpe_id, host, count)


def execute_traceroute_diagnostic(
    db: Session, cpe_id: str, *, host: str
) -> ActionResult:
    return CpeActions.run_traceroute_diagnostic(db, cpe_id, host)
