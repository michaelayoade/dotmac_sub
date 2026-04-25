"""Inventory management for ONT web actions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
from app.services.network.ont_inventory import (
    emit_bundle_unassignment_events,
    reset_ont_service_state,
)
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
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return ActionResult(success=False, message="ONT not found")
    from app.services.network.ont_inventory import return_ont_to_inventory

    result = return_ont_to_inventory(db, ont_id)
    _log_action_audit(
        db,
        request=request,
        action="return_to_inventory",
        ont_id=ont.id,
        metadata={
            "serial_number": ont.serial_number,
            "success": result.success,
        },
        status_code=200 if result.success else 400,
        is_success=result.success,
    )
    return result


def _cleanup_olt_state_for_return(
    db: Session, ont_id: str
) -> tuple[bool, list[str], list[str]]:
    """Remove service ports and deauthorize ONT from OLT.

    Returns:
        (success, completed_steps, errors)
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    completed: list[str] = []
    errors: list[str] = []

    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return False, completed, ["ONT not found"]
    if not olt or not fsp or olt_ont_id is None:
        # No OLT context to clean up - that's OK
        return True, completed, errors

    adapter = get_protocol_adapter(olt)
    ports_result = adapter.get_service_ports_for_ont(fsp, olt_ont_id)
    if not ports_result.success:
        errors.append(f"Cannot read OLT service-ports: {ports_result.message}")
        return False, completed, errors
    service_ports_data = ports_result.data.get("service_ports", [])
    service_ports = service_ports_data if isinstance(service_ports_data, list) else []

    for service_port in service_ports:
        delete_result = adapter.delete_service_port(service_port.index)
        if not delete_result.success:
            errors.append(
                f"Failed to remove service-port {service_port.index}: {delete_result.message}"
            )
            return False, completed, errors
        completed.append(f"Removed service-port {service_port.index}")

    # Release service-port DB allocations to prevent pool exhaustion
    from app.services.network.service_port_allocator import release_all_for_ont

    released_allocations = release_all_for_ont(db, ont_id)
    if released_allocations:
        completed.append(f"Released {released_allocations} service-port allocation(s)")

    deauth_result = adapter.deauthorize_ont(fsp, olt_ont_id)
    if not deauth_result.success:
        errors.append(f"Failed to deauthorize ONT: {deauth_result.message}")
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
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

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

    adapter = get_protocol_adapter(olt)
    ports_result = adapter.get_service_ports_for_ont(fsp, olt_ont_id)
    if not ports_result.success:
        return ActionResult(
            success=False,
            message=f"Cannot read OLT service-ports before release: {ports_result.message}",
        )
    service_ports_data = ports_result.data.get("service_ports", [])
    service_ports = service_ports_data if isinstance(service_ports_data, list) else []

    deleted_service_ports = 0
    for service_port in service_ports:
        delete_result = adapter.delete_service_port(service_port.index)
        if not delete_result.success:
            return ActionResult(
                success=False,
                message=(
                    f"Failed to remove OLT service-port {service_port.index}: {delete_result.message}"
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

    # Release service-port DB allocations to prevent pool exhaustion
    from app.services.network.service_port_allocator import release_all_for_ont

    released_allocations = release_all_for_ont(db, ont_id)
    if released_allocations:
        logger.info(
            "Released %d service-port allocation(s) for ONT %s",
            released_allocations,
            ont_id,
        )

    deauth_result = adapter.deauthorize_ont(fsp, olt_ont_id)
    if not deauth_result.success:
        return ActionResult(
            success=False,
            message=f"Failed to delete ONT from OLT: {deauth_result.message}",
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

    # Clean up TR-069/ACS binding to prevent configuration leakage
    try:
        from app.models.tr069 import Tr069CpeDevice

        tr069_device = db.scalars(
            select(Tr069CpeDevice).where(Tr069CpeDevice.ont_unit_id == ont.id)
        ).first()
        if tr069_device:
            tr069_device.ont_unit_id = None
            logger.info(
                "Cleared TR-069 device %s association for returned ONT %s",
                tr069_device.id,
                ont_id,
            )
    except Exception as e:
        logger.warning("Failed to clear TR-069 device association: %s", e)

    # Use savepoint to enable rollback on partial failure
    deferred_bundle_events: list[dict] = []
    try:
        with db.begin_nested():
            active_assignment = db.scalars(
                select(OntAssignment)
                .where(
                    OntAssignment.ont_unit_id == ont.id,
                    OntAssignment.active.is_(True),
                )
                .order_by(OntAssignment.created_at.desc())
                .limit(1)
                .with_for_update()  # Lock to prevent concurrent modifications
            ).first()

            if active_assignment is not None:
                active_assignment.active = False
                active_assignment.released_at = datetime.now(UTC)
                active_assignment.release_reason = "returned_to_inventory"

            ont.is_active = True  # Keep active for re-use (use decommission for full removal)
            ont.olt_device_id = None
            ont.board = None
            ont.port = None
            ont.external_id = None
            # Defer event emission to avoid commit inside savepoint
            deferred_bundle_events = reset_ont_service_state(
                db, ont, reason="return_to_inventory", emit_events=False
            )
            db.flush()
            ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "Failed to update DB state during return-to-inventory for ONT %s: %s",
            ont_id,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"OLT cleanup succeeded but DB update failed: {exc}. Manual cleanup may be required.",
        )

    # Emit deferred bundle unassignment events after successful commit
    if deferred_bundle_events:
        emit_bundle_unassignment_events(db, deferred_bundle_events)

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
