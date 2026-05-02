"""Device lifecycle actions for ONT web actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import network as network_service
from app.services.genieacs_service import genieacs_service
from app.services.genieacs_service_intent import genieacs_service_intent
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.ont_actions import ActionResult, OntActions
from app.services.network_operations import run_tracked_action
from app.services.web_network_ont_actions._common import (
    _log_action_audit,
    actor_name_from_request,
)

logger = logging.getLogger(__name__)


def execute_reboot(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Execute reboot action with operation tracking."""
    initiated_by = initiated_by or actor_name_from_request(request)
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_reboot,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.reboot(db, ont_id),
        correlation_key=f"ont_reboot:{ont_id}",
        initiated_by=initiated_by,
    )

    # Emit audit event for reboot operation
    if result.success:
        try:
            ont = network_service.ont_units.get_including_inactive(
                db=db, entity_id=ont_id
            )
            emit_event(
                db,
                EventType.ont_rebooted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(ont.olt_device_id)
                    if ont and ont.olt_device_id
                    else None,
                    "method": "tr069",
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_rebooted event: %s", e)

    _log_action_audit(
        db,
        request=request,
        action="reboot",
        ont_id=ont_id,
        metadata={"success": result.success, "message": result.message},
    )
    return result


def execute_refresh(
    db: Session, ont_id: str, *, request: Request | None = None
) -> ActionResult:
    """Execute status refresh and return result."""
    result = OntActions.refresh_status(db, ont_id)
    if result.success:
        db.commit()
    _log_action_audit(
        db,
        request=request,
        action="refresh",
        ont_id=ont_id,
        metadata={"success": result.success},
    )
    return result


def execute_config_snapshot_refresh(
    db: Session, ont_id: str, *, request: Request | None = None
) -> ActionResult:
    """Fetch live TR-069 config and persist the last-known snapshot."""
    summary = genieacs_service_intent.refresh_observed_summary_for_ont(
        db, ont_id=ont_id
    )
    success = bool(summary.available and summary.source == "live" and not summary.error)
    message = (
        "Last known config refreshed."
        if success
        else summary.error or "Unable to refresh last known config."
    )
    _log_action_audit(
        db,
        request=request,
        action="refresh_config_snapshot",
        ont_id=ont_id,
        metadata={
            "success": success,
            "source": summary.source,
            "message": message,
        },
        status_code=200 if success else 502,
        is_success=success,
    )
    return ActionResult(success=success, message=message)


def execute_factory_reset(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Execute factory reset with operation tracking."""
    initiated_by = initiated_by or actor_name_from_request(request)
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_factory_reset,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.factory_reset(db, ont_id),
        correlation_key=f"ont_factory_reset:{ont_id}",
        initiated_by=initiated_by,
    )

    # Emit audit event for factory reset operation
    if result.success:
        try:
            ont = network_service.ont_units.get_including_inactive(
                db=db, entity_id=ont_id
            )
            emit_event(
                db,
                EventType.ont_factory_reset,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(ont.olt_device_id)
                    if ont and ont.olt_device_id
                    else None,
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_factory_reset event: %s", e)

    _log_action_audit(
        db,
        request=request,
        action="factory_reset",
        ont_id=ont_id,
        metadata={"success": result.success, "message": result.message},
    )
    return result


def execute_omci_reboot(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> tuple[bool, str]:
    """Reboot ONT via OMCI through the OLT."""
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"

    reboot_result = get_protocol_adapter(olt).reboot_ont(fsp, olt_ont_id)
    ok = reboot_result.success
    msg = reboot_result.message

    # Emit audit event for reboot operation
    if ok:
        try:
            emit_event(
                db,
                EventType.ont_rebooted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "fsp": fsp,
                    "ont_id_on_olt": olt_ont_id,
                    "method": "omci",
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_rebooted event: %s", e)

    return ok, msg


def execute_connection_request(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Send a TR-069 connection request with operation tracking."""
    initiated_by = initiated_by or actor_name_from_request(request)
    return run_tracked_action(
        db,
        NetworkOperationType.ont_send_conn_request,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: genieacs_service.send_connection_request(db, ont_id),
        correlation_key=f"ont_conn_req:{ont_id}",
        initiated_by=initiated_by,
    )


def execute_reauthorize(
    db: Session,
    ont_id: str,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Re-authorize ONT on OLT with force mode.

    Resolves ONT context and calls OLT authorization service.
    Commits on success.
    """
    from app.models.network import OntUnit
    from app.services.network.ont_authorization import authorize_ont

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    if not ont.olt_device_id:
        return ActionResult(success=False, message="ONT not assigned to an OLT")

    # Build FSP from board/port
    fsp = f"{ont.board}/{ont.port}" if ont.board and ont.port else None
    if not fsp:
        return ActionResult(success=False, message="ONT missing port assignment (FSP)")

    # Call authorize with force=True
    auth_result = authorize_ont(
        db,
        str(ont.olt_device_id),
        fsp,
        ont.serial_number or "",
        force_reauthorize=True,
        request=request,
    )
    auth_ok = auth_result.success
    auth_msg = auth_result.message

    if auth_ok:
        db.commit()

    _log_action_audit(
        db,
        request=request,
        action="reauthorize",
        ont_id=ont_id,
        metadata={"success": auth_ok, "message": auth_msg, "fsp": fsp},
    )

    return ActionResult(success=auth_ok, message=auth_msg)


@dataclass
class RunningConfigResult:
    """Result of fetching ONT running config from OLT."""

    ont: object | None
    olt: object | None
    config_text: str
    error: str | None
    from_cache: bool
    fetched_at: datetime | None


def fetch_olt_running_config(
    db: Session,
    ont_id: str,
) -> RunningConfigResult:
    """Fetch ONT-specific configuration from the OLT via SSH.

    Returns the service-port and ONT info from the OLT CLI.
    Falls back to cached data if OLT is unreachable.
    """
    from app.models.network import OntUnit
    from app.services import web_network_ont_assignments as assignments_service
    from app.services.common import coerce_uuid
    from app.services.network.olt_read_cache import olt_cache
    from app.services.network.olt_ssh import run_cli_command
    from app.services.network.serial_utils import parse_ont_id_on_olt

    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        return RunningConfigResult(
            ont=None,
            olt=None,
            config_text="",
            error="ONT not found",
            from_cache=False,
            fetched_at=None,
        )

    active_assignment = assignments_service.active_assignment_for_ont_id(db, ont.id)

    # Try to get the OLT from the ONT or active assignment
    olt = None
    if ont.olt_device:
        olt = ont.olt_device
    elif active_assignment and active_assignment.pon_port:
        olt = active_assignment.pon_port.olt

    if not olt:
        return RunningConfigResult(
            ont=ont,
            olt=None,
            config_text="",
            error="No OLT associated with this ONT",
            from_cache=False,
            fetched_at=None,
        )

    # Build the ONT-specific command (Huawei style)
    fsp = None
    onu_id = None
    if active_assignment and active_assignment.pon_port:
        pon = active_assignment.pon_port
        fsp = pon.name  # e.g., "0/1/0"
        onu_id = parse_ont_id_on_olt(getattr(ont, "external_id", None))

    # Cache key for this ONT's config
    cache_key = f"ont_config:{ont_id}"
    cached = olt_cache.get(str(olt.id), "cli", cache_key)

    config_lines: list[str] = []
    error_msg = None
    from_cache = False

    # Try to fetch service-port info for this ONT
    if fsp and onu_id:
        # Run display service-port filtered by ONT
        cmd = f"display service-port port {fsp} ont {onu_id}"
        ok, msg, output = run_cli_command(olt, cmd)
        if ok and output.strip():
            config_lines.append(f"# Service Ports ({cmd})")
            config_lines.append(output.strip())
            config_lines.append("")
        elif not ok and cached:
            from_cache = True
            config_lines.append(cached)
            error_msg = "OLT unreachable - showing cached config"
        else:
            error_msg = msg

        # Also try to get ONT info
        cmd2 = f"display ont info {fsp} {onu_id}"
        ok2, _, output2 = run_cli_command(olt, cmd2)
        if ok2 and output2.strip():
            config_lines.append(f"# ONT Info ({cmd2})")
            config_lines.append(output2.strip())
    else:
        # Fallback: try by serial number
        cmd = f"display ont info by-sn {ont.serial_number}"
        ok, msg, output = run_cli_command(olt, cmd)
        if ok and output.strip():
            config_lines.append(f"# ONT Info ({cmd})")
            config_lines.append(output.strip())
        elif not ok and cached:
            from_cache = True
            config_lines.append(cached)
            error_msg = "OLT unreachable - showing cached config"
        else:
            error_msg = msg

    config_text = "\n".join(config_lines)

    # Cache successful results
    if config_text and not from_cache and not error_msg:
        olt_cache.set(str(olt.id), "cli", config_text, cache_key)

    return RunningConfigResult(
        ont=ont,
        olt=olt,
        config_text=config_text,
        error=error_msg,
        from_cache=from_cache,
        fetched_at=datetime.now(UTC),
    )
