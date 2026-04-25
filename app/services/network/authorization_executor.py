"""Execution adapter for ONT authorization.

Provides synchronous authorization execution with immediate success/failure.

Usage:
    result = execute_authorization(
        db, olt_id, fsp, serial_number,
        force_reauthorize=False,
        request=request,
    )
    if result.success:
        print(f"ONT ID: {result.ont_id}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


class AuthorizationExecutionMode(Enum):
    sync_success = "sync_success"
    sync_error = "sync_error"


@dataclass
class AuthorizationExecutionResult:
    """Result from authorization execution."""

    success: bool
    message: str
    ont_id: str | None = None
    serial_number: str | None = None
    fsp: str | None = None
    error: str | None = None
    mode: AuthorizationExecutionMode = AuthorizationExecutionMode.sync_success
    operation_id: str | None = None
    details: dict = field(default_factory=dict)

    def to_operation_result(self):
        """Convert to OperationResult for response rendering."""
        from app.services.network.result_adapter import OperationResult, ResultStatus

        data = {
            "ont_id": self.ont_id,
            "serial_number": self.serial_number,
            "fsp": self.fsp,
        }
        data.update(self.details)

        if not self.success:
            return OperationResult(
                status=ResultStatus.error,
                message=self.message,
                title="Authorization Failed",
                data={k: v for k, v in data.items() if v is not None},
            )

        return OperationResult(
            status=ResultStatus.success,
            message=self.message,
            title="Authorization Complete",
            data={k: v for k, v in data.items() if v is not None},
        )


def execute_authorization(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    initiated_by: str | None = None,
    request: Request | None = None,
    preset_id: str | None = None,
    prefer_sync: bool = False,
) -> AuthorizationExecutionResult:
    """Execute ONT authorization synchronously.

    Args:
        db: Database session
        olt_id: OLT device ID
        fsp: Frame/Slot/Port location (e.g., "0/1/0")
        serial_number: ONT serial number
        force_reauthorize: If True, delete existing registration first
        initiated_by: User/system that initiated the action (unused, for compatibility)
        request: HTTP request for audit logging
        preset_id: Optional provisioning preset ID

    Returns:
        AuthorizationExecutionResult with execution details
    """
    # Validate inputs
    if not fsp or not serial_number:
        return AuthorizationExecutionResult(
            success=False,
            message="Missing port (FSP) or serial number",
            fsp=fsp,
            serial_number=serial_number,
            error="validation_error",
            mode=AuthorizationExecutionMode.sync_error,
        )

    # Normalize serial number
    normalized_serial = str(serial_number).replace("-", "").strip().upper()

    from app.services.network.ont_authorization import (
        authorize_autofind_ont_and_provision_network_audited,
    )

    try:
        result = authorize_autofind_ont_and_provision_network_audited(
            db,
            olt_id,
            fsp,
            normalized_serial,
            force_reauthorize=force_reauthorize,
            preset_id=preset_id,
            request=request,
        )

        return AuthorizationExecutionResult(
            success=result.success,
            message=result.message,
            ont_id=result.ont_unit_id,
            serial_number=normalized_serial,
            fsp=fsp,
            mode=(
                AuthorizationExecutionMode.sync_success
                if result.success
                else AuthorizationExecutionMode.sync_error
            ),
            details={
                "ont_id_on_olt": result.ont_id_on_olt,
            },
        )

    except Exception as exc:
        logger.error(
            "Authorization failed for %s on %s: %s",
            normalized_serial,
            olt_id,
            exc,
            exc_info=True,
        )
        db.rollback()
        return AuthorizationExecutionResult(
            success=False,
            message=f"Authorization failed: {exc}",
            serial_number=normalized_serial,
            fsp=fsp,
            error=str(exc),
            mode=AuthorizationExecutionMode.sync_error,
        )


def execute_authorization_batch(
    db: Session,
    authorizations: list[dict],
    *,
    force_reauthorize: bool = False,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> list[AuthorizationExecutionResult]:
    """Execute multiple authorizations synchronously.

    Args:
        db: Database session
        authorizations: List of dicts with keys: olt_id, fsp, serial_number
        force_reauthorize: If True, delete existing registrations first
        initiated_by: User/system that initiated the action
        request: HTTP request for audit logging

    Returns:
        List of AuthorizationExecutionResult, one per input
    """
    results = []
    for auth in authorizations:
        result = execute_authorization(
            db,
            auth["olt_id"],
            auth["fsp"],
            auth["serial_number"],
            force_reauthorize=force_reauthorize,
            initiated_by=initiated_by,
            request=request,
        )
        results.append(result)
    return results
