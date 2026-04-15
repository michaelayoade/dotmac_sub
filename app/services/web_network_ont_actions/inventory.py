"""Inventory management for ONT web actions."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OntAssignment, OntProvisioningStatus
from app.services import network as network_service
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.cpe import ensure_cpe_for_ont
from app.services.network.ont_actions import ActionResult
from app.services.web_network_ont_actions._common import (
    _log_action_audit,
    _resolve_return_olt_context,
    actor_name_from_request,
)

logger = logging.getLogger(__name__)


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
    previous_olt_id = str(olt.id)
    previous_fsp = fsp

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

    ont.is_active = True  # Keep active for re-use (use decommission for full removal)
    ont.olt_device_id = None
    ont.board = None
    ont.port = None
    ont.provisioning_profile_id = None
    ont.provisioning_status = OntProvisioningStatus.unprovisioned
    ont.authorization_status = None
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
    # Clear LAN configuration
    ont.lan_gateway_ip = None
    ont.lan_subnet_mask = None
    ont.lan_dhcp_enabled = None
    ont.lan_dhcp_start = None
    ont.lan_dhcp_end = None
    db.flush()
    ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)

    db.commit()
    db.refresh(ont)

    from app.services.web_network_ont_autofind import refresh_returned_ont_autofind

    autofind_refresh = refresh_returned_ont_autofind(
        db,
        olt_id=previous_olt_id,
        serial_number=ont.serial_number,
        fsp=previous_fsp,
    )

    assignment_msg = "assignment closed and " if active_assignment is not None else ""
    service_port_msg = (
        f"{deleted_service_ports} service-port(s) removed, "
        if deleted_service_ports
        else ""
    )
    autofind_msg = (
        " Autofind refreshed and ONT is visible in the unconfigured list."
        if autofind_refresh.get("rediscovered")
        else " Autofind refreshed; ONT will appear in the unconfigured list when rediscovered."
        if autofind_refresh.get("ok")
        else f" Autofind refresh failed: {autofind_refresh.get('message')}."
    )
    result = ActionResult(
        success=True,
        message=(
            "ONT returned to inventory: "
            f"{service_port_msg}{assignment_msg}removed from OLT and service state cleared."
            f"{autofind_msg}"
        ),
        data={
            "olt_id": previous_olt_id,
            "olt_name": olt.name,
            "fsp": previous_fsp,
            "serial_number": ont.serial_number,
            "autofind_refreshed": autofind_refresh.get("ok"),
            "autofind_rediscovered": autofind_refresh.get("rediscovered"),
            "unconfigured_url": autofind_refresh.get("url"),
        },
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
    from app.services.network.ont_actions import OntActions

    result = OntActions.firmware_upgrade(db, ont_id, firmware_image_id)
    _log_action_audit(
        db,
        request=request,
        action="firmware_upgrade",
        ont_id=ont_id,
        metadata={"firmware_image_id": firmware_image_id, "success": result.success},
    )
    return result
