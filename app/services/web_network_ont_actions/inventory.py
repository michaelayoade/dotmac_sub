"""Inventory management for ONT web actions."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import network as network_service
from app.services.network.ont_actions import ActionResult

# Re-exports for backward compatibility with callers that still import
# these names through ``app.services.web_network_ont_actions.inventory``.
from app.services.network.ont_inventory import (
    cleanup_olt_state_for_return as _cleanup_olt_state_for_return,
)
from app.services.network.ont_inventory import (
    return_ont_to_inventory as return_to_inventory,
)
from app.services.network_operations import network_operations
from app.services.web_network_ont_actions._common import (
    _log_action_audit,
    actor_name_from_request,
)

__all__ = (
    "_cleanup_olt_state_for_return",
    "firmware_upgrade",
    "return_to_inventory",
    "return_to_inventory_for_web",
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

    actor = actor_name_from_request(request)
    try:
        operation = network_operations.start(
            db,
            NetworkOperationType.ont_return_to_inventory,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=f"ont_return_to_inventory:{ont_id}",
            input_payload={"serial_number": ont.serial_number},
            initiated_by=actor,
        )
        network_operations.mark_running(db, str(operation.id))
        db.commit()
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return ActionResult(
            success=False,
            message="A return-to-inventory operation is already in progress.",
        )

    try:
        result = return_ont_to_inventory(db, ont_id)
    except Exception as exc:
        db.rollback()
        network_operations.mark_failed(
            db,
            str(operation.id),
            f"Unexpected return-to-inventory failure: {exc}",
        )
        db.commit()
        raise
    output = {
        "success": result.success,
        "message": result.message,
        "result": result.data or {},
    }
    if result.success:
        network_operations.mark_succeeded(db, str(operation.id), output_payload=output)
    else:
        network_operations.mark_failed(
            db,
            str(operation.id),
            result.message,
            output_payload=output,
        )
    db.commit()
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


def firmware_upgrade(
    db: Session, ont_id: str, firmware_image_id: str, *, request: Request | None = None
) -> ActionResult:
    """Trigger firmware upgrade and audit the admin action."""
    from app.services.network.ont_firmware import request_firmware_upgrade

    result = request_firmware_upgrade(
        db,
        ont_id,
        firmware_image_id,
        initiated_by=actor_name_from_request(request),
    )
    _log_action_audit(
        db,
        request=request,
        action="firmware_upgrade",
        ont_id=ont_id,
        metadata={"firmware_image_id": firmware_image_id, "success": result.success},
        status_code=202 if result.waiting else 200 if result.success else 400,
    )
    return result
