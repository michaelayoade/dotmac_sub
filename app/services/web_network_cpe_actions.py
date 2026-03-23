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
