"""Service helpers for remote ONT action web routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, OntAssignment, OntProvisioningStatus, OntUnit
from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import network as network_service
from app.services.audit_helpers import log_audit_event
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.cpe import ensure_cpe_for_ont
from app.services.network.ont_actions import ActionResult, OntActions
from app.services.network_operations import run_tracked_action

logger = logging.getLogger(__name__)


def _current_user(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    from app.web.admin import get_current_user

    return get_current_user(request)


def actor_name_from_request(request: Request | None) -> str:
    current_user = _current_user(request)
    return str(current_user.get("name", "unknown")) if current_user else "system"


def _actor_id_from_request(request: Request | None) -> str | None:
    current_user = _current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def _log_action_audit(
    db: Session,
    *,
    request: Request | None,
    action: str,
    ont_id: object,
    metadata: dict[str, object] | None = None,
    status_code: int | None = None,
    is_success: bool = True,
) -> None:
    if request is None:
        return
    kwargs: dict[str, object] = {}
    if status_code is not None:
        kwargs["status_code"] = status_code
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=_actor_id_from_request(request),
        metadata=metadata,
        is_success=is_success,
        **kwargs,
    )


def _normalize_fsp(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw.lower().startswith("pon-"):
        raw = raw[4:].strip()
    return raw or None


def _parse_ont_id_on_olt(external_id: str | None) -> int | None:
    ext = (external_id or "").strip()
    if ext.isdigit():
        return int(ext)
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    if ":" in ext:
        suffix = ext.rsplit(":", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _resolve_return_olt_context(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, OLTDevice | None, str | None, int | None]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)

    olt = db.get(OLTDevice, str(ont.olt_device_id)) if ont.olt_device_id else None
    board = (ont.board or "").strip()
    port = (ont.port or "").strip()
    fsp = _normalize_fsp(f"{board}/{port}") if board and port else None
    ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)
    return ont, olt, fsp, ont_id_on_olt


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
    _log_action_audit(
        db,
        request=request,
        action="refresh",
        ont_id=ont_id,
        metadata={"success": result.success},
    )
    return result


def fetch_running_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch running config and return structured result."""
    return OntActions.get_running_config(db, ont_id)


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


def set_wifi_ssid(
    db: Session, ont_id: str, ssid: str, *, request: Request | None = None
) -> ActionResult:
    """Set WiFi SSID and return result."""
    result = OntActions.set_wifi_ssid(db, ont_id, ssid)
    _log_action_audit(
        db,
        request=request,
        action="set_wifi_ssid",
        ont_id=ont_id,
        metadata={"success": result.success, "ssid": ssid},
    )
    return result


def set_wifi_password(
    db: Session, ont_id: str, password: str, *, request: Request | None = None
) -> ActionResult:
    """Set WiFi password and return result."""
    result = OntActions.set_wifi_password(db, ont_id, password)
    _log_action_audit(
        db,
        request=request,
        action="set_wifi_password",
        ont_id=ont_id,
        metadata={"success": result.success},
    )
    return result


def toggle_lan_port(
    db: Session,
    ont_id: str,
    port: int,
    enabled: bool,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Toggle a LAN port and return result."""
    result = OntActions.toggle_lan_port(db, ont_id, port, enabled)
    _log_action_audit(
        db,
        request=request,
        action="toggle_lan_port",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "port": port,
            "enabled": enabled,
        },
    )
    return result


def set_lan_config(
    db: Session,
    ont_id: str,
    *,
    lan_ip: str | None = None,
    lan_subnet: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Set LAN IP/subnet on ONT via GenieACS TR-069."""
    result = OntActions.set_lan_config(db, ont_id, lan_ip=lan_ip, lan_subnet=lan_subnet)
    _log_action_audit(
        db,
        request=request,
        action="set_lan_config",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "lan_ip": lan_ip,
            "lan_subnet": lan_subnet,
        },
    )
    return result


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    username: str,
    password: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Push PPPoE credentials to ONT via TR-069 with operation tracking."""
    initiated_by = initiated_by or actor_name_from_request(request)
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_set_pppoe,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.set_pppoe_credentials(db, ont_id, username, password),
        correlation_key=f"ont_set_pppoe:{ont_id}",
        initiated_by=initiated_by,
    )
    waiting = getattr(result, "waiting", False)
    _log_action_audit(
        db,
        request=request,
        action="set_pppoe_credentials",
        ont_id=ont_id,
        metadata={
            "result": "success"
            if result.success
            else ("waiting" if waiting else "error"),
            "message": result.message,
            "username": username,
        },
        status_code=200 if result.success else (202 if waiting else 500),
        is_success=result.success or waiting,
    )
    return result


def run_ping_diagnostic(
    db: Session,
    ont_id: str,
    host: str,
    count: int = 4,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Run ping diagnostic from ONT via TR-069."""
    result = OntActions.run_ping_diagnostic(db, ont_id, host, count)
    _log_action_audit(
        db,
        request=request,
        action="ping_diagnostic",
        ont_id=ont_id,
        metadata={
            "result": "success" if result.success else "error",
            "host": host,
            "count": count,
        },
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def run_traceroute_diagnostic(
    db: Session, ont_id: str, host: str, *, request: Request | None = None
) -> ActionResult:
    """Run traceroute diagnostic from ONT via TR-069."""
    result = OntActions.run_traceroute_diagnostic(db, ont_id, host)
    _log_action_audit(
        db,
        request=request,
        action="traceroute_diagnostic",
        ont_id=ont_id,
        metadata={"result": "success" if result.success else "error", "host": host},
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def execute_enable_ipv6(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Enable IPv6 dual-stack on ONT with operation tracking."""
    from app.services.network.ont_action_network import enable_ipv6_on_wan

    initiated_by = initiated_by or actor_name_from_request(request)
    return run_tracked_action(
        db,
        NetworkOperationType.ont_enable_ipv6,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: enable_ipv6_on_wan(db, ont_id),
        correlation_key=f"ont_enable_ipv6:{ont_id}",
        initiated_by=initiated_by,
    )


def execute_omci_reboot(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> tuple[bool, str]:
    """Reboot ONT via OMCI through the OLT."""
    from app.services.network.olt_ssh_ont import reboot_ont_omci
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"

    ok, msg = reboot_ont_omci(olt, fsp, olt_ont_id)

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


def configure_management_ip(
    db: Session,
    ont_id: str,
    vlan_id: int,
    ip_mode: str = "dhcp",
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP via OLT IPHOST command."""
    from app.services.network.olt_ssh_ont import configure_ont_iphost
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    return configure_ont_iphost(
        olt,
        fsp,
        olt_ont_id,
        vlan_id=vlan_id,
        ip_mode=ip_mode,
        ip_address=ip_address,
        subnet=subnet,
        gateway=gateway,
    )


def fetch_iphost_config(db: Session, ont_id: str) -> tuple[bool, str, dict[str, str]]:
    """Fetch ONT IPHOST config from OLT."""
    from app.services.network.olt_ssh_ont import get_ont_iphost_config
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT", {}
    return get_ont_iphost_config(olt, fsp, olt_ont_id)


def bind_tr069_profile(db: Session, ont_id: str, profile_id: int) -> tuple[bool, str]:
    """Bind TR-069 server profile to ONT via OLT."""
    from app.services.network.olt_ssh_ont import bind_tr069_server_profile
    from app.services.network.ont_provision_steps import queue_wait_tr069_bootstrap
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    ok, message = bind_tr069_server_profile(olt, fsp, olt_ont_id, profile_id)
    if ok:
        try:
            wait_result = queue_wait_tr069_bootstrap(db, ont_id)
            message = f"{message}; {wait_result.message}"
        except Exception as exc:
            logger.warning(
                "Failed to queue TR-069 bootstrap wait after manual bind for ONT %s: %s",
                ont_id,
                exc,
            )
            message = f"{message}; failed to queue ACS inform wait: {exc}"
    return ok, message


def iphost_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build management IP config context for the ONT detail partial."""
    from app.services import web_network_onts as web_network_onts_service
    from app.services.network import ont_web_forms as ont_web_forms_service

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    ok, msg, config = fetch_iphost_config(db, ont_id)
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    tr069_profiles, tr069_profiles_error = (
        web_network_onts_service.get_tr069_profiles_for_ont(db, ont)
    )
    return {
        "ont": ont,
        "iphost_config": config,
        "iphost_ok": ok,
        "iphost_msg": msg,
        "initial_iphost_form": ont_web_forms_service.initial_iphost_form(ont, config),
        "vlans": vlans,
        "tr069_profiles": tr069_profiles,
        "tr069_profiles_error": tr069_profiles_error,
    }


def return_to_inventory_for_web(
    db: Session,
    ont_id: str,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Return ONT to inventory with route-friendly not-found handling."""
    try:
        network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return ActionResult(success=False, message="ONT not found")
    return return_to_inventory(db, ont_id, request=request)


def _config_snapshot_service():
    try:
        from app.services.network.ont_config_snapshots import ont_config_snapshots
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail="Config snapshots not available",
        ) from exc
    return ont_config_snapshots


def capture_config_snapshot_list_context(
    db: Session,
    *,
    ont_id: str,
    label: str | None,
    limit: int = 5,
) -> tuple[dict[str, object], str | None]:
    """Capture a config snapshot and return refreshed list context plus error."""
    snapshots_service = _config_snapshot_service()
    error_msg: str | None = None
    try:
        snapshots_service.capture(db, ont_id, label=label)
    except HTTPException as exc:
        error_msg = str(exc.detail)
    return {
        "ont_id": ont_id,
        "config_snapshots": snapshots_service.list_for_ont(db, ont_id, limit=limit),
    }, error_msg


def config_snapshot_detail_context(
    db: Session,
    *,
    ont_id: str,
    snapshot_id: str,
) -> dict[str, object]:
    """Return context for a single ONT config snapshot detail."""
    snapshot = _config_snapshot_service().get(db, snapshot_id, ont_id=ont_id)
    return {"snapshot": snapshot}


def delete_config_snapshot_list_context(
    db: Session,
    *,
    ont_id: str,
    snapshot_id: str,
    limit: int = 5,
) -> dict[str, object]:
    """Delete a config snapshot and return refreshed list context."""
    snapshots_service = _config_snapshot_service()
    snapshots_service.delete(db, snapshot_id, ont_id=ont_id)
    return {
        "ont_id": ont_id,
        "config_snapshots": snapshots_service.list_for_ont(db, ont_id, limit=limit),
    }


def _cleanup_olt_state_for_return(
    db: Session, ont_id: str
) -> tuple[bool, list[str], list[str]]:
    """Remove service ports and deauthorize ONT from OLT.

    Returns:
        (success, completed_steps, errors)
    """
    from app.services.network.olt_ssh_ont import deauthorize_ont
    from app.services.network.olt_ssh_service_ports import (
        delete_service_port,
        get_service_ports_for_ont,
    )

    completed: list[str] = []
    errors: list[str] = []

    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return False, completed, ["ONT not found"]
    if not olt or not fsp or olt_ont_id is None:
        # No OLT context to clean up - that's OK
        return True, completed, errors

    ok, msg, service_ports = get_service_ports_for_ont(olt, fsp, olt_ont_id)
    if not ok:
        errors.append(f"Cannot read OLT service-ports: {msg}")
        return False, completed, errors

    for service_port in service_ports:
        ok, msg = delete_service_port(olt, service_port.index)
        if not ok:
            errors.append(f"Failed to remove service-port {service_port.index}: {msg}")
            return False, completed, errors
        completed.append(f"Removed service-port {service_port.index}")

    ok, msg = deauthorize_ont(olt, fsp, olt_ont_id)
    if not ok:
        errors.append(f"Failed to deauthorize ONT: {msg}")
        return False, completed, errors
    completed.append("Deauthorized ONT from OLT")

    return True, completed, errors


def return_to_inventory(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Release an ONT from the OLT, close assignment, and clear service state."""
    from app.services.network.olt_ssh_ont import deauthorize_ont
    from app.services.network.olt_ssh_service_ports import (
        delete_service_port,
        get_service_ports_for_ont,
    )

    initiated_by = initiated_by or actor_name_from_request(request)
    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return ActionResult(success=False, message="ONT not found.")
    if not olt or not fsp or olt_ont_id is None:
        return ActionResult(
            success=False,
            message="Cannot resolve OLT context for this ONT.",
        )

    ok, msg, service_ports = get_service_ports_for_ont(olt, fsp, olt_ont_id)
    if not ok:
        return ActionResult(
            success=False,
            message=f"Cannot read OLT service-ports before release: {msg}",
        )

    deleted_service_ports = 0
    for service_port in service_ports:
        ok, msg = delete_service_port(olt, service_port.index)
        if not ok:
            return ActionResult(
                success=False,
                message=(
                    f"Failed to remove OLT service-port {service_port.index}: {msg}"
                ),
            )
        deleted_service_ports += 1

        # Emit audit event for service port deletion
        try:
            emit_event(
                db,
                EventType.ont_service_port_deleted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "service_port_index": service_port.index,
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_service_port_deleted event: %s", e)

    ok, msg = deauthorize_ont(olt, fsp, olt_ont_id)
    if not ok:
        return ActionResult(
            success=False,
            message=f"Failed to delete ONT from OLT: {msg}",
        )

    # Emit audit event for ONT deauthorization
    try:
        emit_event(
            db,
            EventType.ont_deauthorized,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                "fsp": fsp,
                "ont_id_on_olt": olt_ont_id,
            },
            actor=initiated_by or "system",
        )
    except Exception as e:
        logger.warning("Failed to emit ont_deauthorized event: %s", e)

    active_assignment = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.created_at.desc())
        .limit(1)
    ).first()

    if active_assignment is not None:
        active_assignment.active = False

    ont.is_active = False
    ont.provisioning_profile_id = None
    ont.provisioning_status = OntProvisioningStatus.unprovisioned
    ont.last_provisioned_at = None
    ont.external_id = None
    ont.wan_vlan_id = None
    ont.wan_mode = None
    ont.config_method = None
    ont.ip_protocol = None
    ont.pppoe_username = None
    ont.pppoe_password = None
    ont.wan_remote_access = False
    ont.tr069_acs_server_id = None
    ont.mgmt_ip_mode = None
    ont.mgmt_vlan_id = None
    ont.mgmt_ip_address = None
    ont.mgmt_remote_access = False
    ont.voip_enabled = False
    db.flush()
    ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)

    db.commit()
    db.refresh(ont)

    assignment_msg = "assignment closed and " if active_assignment is not None else ""
    service_port_msg = (
        f"{deleted_service_ports} service-port(s) removed, "
        if deleted_service_ports
        else ""
    )
    result = ActionResult(
        success=True,
        message=(
            "ONT returned to inventory: "
            f"{service_port_msg}{assignment_msg}removed from OLT and service state cleared."
        ),
    )
    _log_action_audit(
        db,
        request=request,
        action="return_to_inventory",
        ont_id=ont.id,
        metadata={"serial_number": ont.serial_number},
    )
    return result


def apply_profile(
    db: Session, ont_id: str, profile_id: str, *, request: Request | None = None
) -> Any:
    """Apply a profile template and audit the explicit admin action."""
    from app.services.network.ont_profile_apply import apply_profile_to_ont

    result = apply_profile_to_ont(db, ont_id, profile_id)
    _log_action_audit(
        db,
        request=request,
        action="apply_profile",
        ont_id=ont_id,
        metadata={
            "profile_id": profile_id,
            "success": result.success,
            "fields_updated": result.fields_updated,
        },
    )
    return result


def firmware_upgrade(
    db: Session, ont_id: str, firmware_image_id: str, *, request: Request | None = None
) -> ActionResult:
    """Trigger firmware upgrade and audit the admin action."""
    result = OntActions.firmware_upgrade(db, ont_id, firmware_image_id)
    _log_action_audit(
        db,
        request=request,
        action="firmware_upgrade",
        ont_id=ont_id,
        metadata={"firmware_image_id": firmware_image_id, "success": result.success},
    )
    return result


def execute_connection_request(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Send a TR-069 connection request with operation tracking."""
    from app.services.network.ont_action_network import send_connection_request_tracked

    initiated_by = initiated_by or actor_name_from_request(request)
    return send_connection_request_tracked(db, ont_id, initiated_by=initiated_by)


def fetch_olt_side_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch ONT config from OLT side via SSH (works without GenieACS).

    Returns an ActionResult with data dict containing ont_info, ont_wan,
    and service_ports sections.
    """
    from app.services.network.ont_action_device import get_running_config

    result = get_running_config(db, ont_id)
    if not result.success:
        return ActionResult(success=False, message=result.message)

    data = result.data or {}
    return ActionResult(
        success=True,
        message="OLT-side config retrieved",
        data={
            "ont_info": data.get("device_info", ""),
            "ont_wan": data.get("wan", ""),
            "service_ports": data.get("service_ports", ""),
        },
    )


def fetch_olt_status(db: Session, ont_id: str) -> dict[str, Any]:
    """Query the OLT directly for ONT registration state (GPON layer).

    Returns a dict with success, message, and optional entry data.
    """
    from app.models.network import OntUnit

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return {"success": False, "message": "ONT not found"}

    olt = getattr(ont, "olt_device", None)
    if not olt:
        return {"success": False, "message": "ONT has no associated OLT"}

    return {
        "success": True,
        "message": "ONT status retrieved",
        "entry": {
            "online_status": getattr(ont, "online_status", None),
            "onu_rx_signal_dbm": getattr(ont, "onu_rx_signal_dbm", None),
            "olt_rx_signal_dbm": getattr(ont, "olt_rx_signal_dbm", None),
        },
    }


def resolve_stored_pppoe_password(db: Session, ont_id: str) -> str:
    """Decrypt and return the stored PPPoE password for an ONT."""
    from app.models.network import OntUnit
    from app.services.credential_crypto import decrypt_credential

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return ""

    raw = getattr(ont, "pppoe_password", None)
    if not raw:
        return ""

    try:
        return decrypt_credential(raw) or ""
    except Exception:
        logger.warning("Failed to decrypt PPPoE password for ONT %s", ont_id)
        return ""


def reveal_stored_pppoe_password(
    db: Session, ont_id: str, *, request: Request | None = None
) -> tuple[str, bool]:
    """Return stored PPPoE password and audit the reveal action."""
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return "", False

    password = resolve_stored_pppoe_password(db, ont_id)
    _log_action_audit(
        db,
        request=request,
        action="reveal_pppoe_password",
        ont_id=ont_id,
        metadata={"username": ont.pppoe_username or ""},
    )
    return password, True
