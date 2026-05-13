"""Inventory management for ONT web actions."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

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
from app.services.web_network_ont_actions._common import (
    _log_action_audit,
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
