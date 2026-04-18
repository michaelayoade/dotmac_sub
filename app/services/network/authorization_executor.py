"""Execution adapter for ONT authorization.

Provides resilient authorization execution with async/sync fallback:
- Tries Celery (async) first for non-blocking operation
- Falls back to synchronous execution if Celery/Redis unavailable
- Returns consistent results either way

Usage:
    result = execute_authorization(
        db, olt_id, fsp, serial_number,
        force_reauthorize=False,
        request=request,
    )
    if result.success:
        print(f"Mode: {result.mode}, Operation ID: {result.operation_id}")
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


class AuthorizationMode(str, Enum):
    """How the authorization was executed."""

    async_queued = "async_queued"  # Queued to Celery, returns immediately
    sync_immediate = "sync_immediate"  # Ran synchronously, blocking
    skipped = "skipped"  # Already running/queued, no action taken


@dataclass
class AuthorizationExecutionResult:
    """Result from authorization execution adapter."""

    success: bool
    message: str
    mode: AuthorizationMode
    operation_id: str | None = None
    ont_id: str | None = None
    serial_number: str | None = None
    fsp: str | None = None
    error: str | None = None
    details: dict = field(default_factory=dict)

    def to_operation_result(self):
        """Convert to OperationResult for response rendering."""
        from app.services.network.result_adapter import OperationResult, ResultStatus

        data = {
            "mode": self.mode.value,
            "operation_id": self.operation_id,
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

        if self.mode == AuthorizationMode.async_queued:
            return OperationResult(
                status=ResultStatus.queued,
                message=self.message,
                title="Authorization Queued",
                data={k: v for k, v in data.items() if v is not None},
            )

        if self.mode == AuthorizationMode.skipped:
            return OperationResult(
                status=ResultStatus.pending,
                message=self.message,
                title="Already In Progress",
                data={k: v for k, v in data.items() if v is not None},
            )

        # sync_immediate success
        return OperationResult(
            status=ResultStatus.success,
            message=self.message,
            title="Authorization Complete",
            data={k: v for k, v in data.items() if v is not None},
        )


def is_celery_available() -> bool:
    """Check if Celery/Redis is available for task queuing."""
    try:
        from app.celery_app import celery_app

        # Try to ping Redis via Celery's connection
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.release()
        return True
    except Exception as exc:
        logger.debug("Celery/Redis not available: %s", exc)
        return False


def execute_authorization(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    initiated_by: str | None = None,
    request: Request | None = None,
    prefer_sync: bool = False,
) -> AuthorizationExecutionResult:
    """Execute ONT authorization with async/sync fallback.

    Args:
        db: Database session
        olt_id: OLT device ID
        fsp: Frame/Slot/Port location (e.g., "0/1/0")
        serial_number: ONT serial number
        force_reauthorize: If True, delete existing registration first
        initiated_by: User/system that initiated the action
        request: HTTP request for audit logging
        prefer_sync: If True, skip async and run synchronously

    Returns:
        AuthorizationExecutionResult with execution details
    """
    from app.services.network.action_logging import actor_label

    if initiated_by is None and request is not None:
        initiated_by = actor_label(request)

    # Validate inputs
    if not fsp or not serial_number:
        return AuthorizationExecutionResult(
            success=False,
            message="Missing port (FSP) or serial number",
            mode=AuthorizationMode.skipped,
            fsp=fsp,
            serial_number=serial_number,
            error="validation_error",
        )

    # Normalize serial number
    normalized_serial = str(serial_number).replace("-", "").strip().upper()

    # Try async first unless prefer_sync is set
    if not prefer_sync and is_celery_available():
        return _execute_async(
            db,
            olt_id,
            fsp,
            normalized_serial,
            force_reauthorize=force_reauthorize,
            initiated_by=initiated_by,
            request=request,
        )

    # Fall back to synchronous execution
    logger.info(
        "Running authorization synchronously (prefer_sync=%s, celery_available=%s)",
        prefer_sync,
        is_celery_available() if not prefer_sync else "skipped",
    )
    return _execute_sync(
        db,
        olt_id,
        fsp,
        normalized_serial,
        force_reauthorize=force_reauthorize,
        initiated_by=initiated_by,
        request=request,
    )


def _execute_async(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool,
    initiated_by: str | None,
    request: Request | None,
) -> AuthorizationExecutionResult:
    """Queue authorization to Celery for async execution."""
    from app.services.network.olt_authorization_workflow import (
        queue_authorize_autofind_ont,
    )

    try:
        ok, message, operation_id = queue_authorize_autofind_ont(
            db,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
            force_reauthorize=force_reauthorize,
            initiated_by=initiated_by,
            request=request,
        )

        if ok:
            # Check if it was already queued (message indicates this)
            if "already queued" in message.lower() or "already running" in message.lower():
                return AuthorizationExecutionResult(
                    success=True,
                    message=message,
                    mode=AuthorizationMode.skipped,
                    operation_id=operation_id,
                    serial_number=serial_number,
                    fsp=fsp,
                )

            return AuthorizationExecutionResult(
                success=True,
                message=message,
                mode=AuthorizationMode.async_queued,
                operation_id=operation_id,
                serial_number=serial_number,
                fsp=fsp,
            )

        # Queuing failed - try sync fallback
        logger.warning(
            "Async queue failed for %s on %s: %s - falling back to sync",
            serial_number,
            olt_id,
            message,
        )
        return _execute_sync(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=force_reauthorize,
            initiated_by=initiated_by,
            request=request,
        )

    except Exception as exc:
        logger.warning(
            "Exception during async queue for %s on %s: %s - falling back to sync",
            serial_number,
            olt_id,
            exc,
        )
        return _execute_sync(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=force_reauthorize,
            initiated_by=initiated_by,
            request=request,
        )


def _execute_sync(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool,
    initiated_by: str | None,
    request: Request | None,
) -> AuthorizationExecutionResult:
    """Execute authorization synchronously (blocking)."""
    from app.services.network.olt_authorization_workflow import (
        authorize_autofind_ont_and_provision_network_audited,
    )

    try:
        result = authorize_autofind_ont_and_provision_network_audited(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=force_reauthorize,
            request=request,
        )

        return AuthorizationExecutionResult(
            success=result.success,
            message=result.message,
            mode=AuthorizationMode.sync_immediate,
            operation_id=None,  # No operation tracking for sync
            ont_id=result.ont_id,
            serial_number=serial_number,
            fsp=fsp,
            details={
                "ont_id_on_olt": result.ont_id_on_olt,
                "provisioning_status": result.provisioning_status.value
                if result.provisioning_status
                else None,
            },
        )

    except Exception as exc:
        logger.error(
            "Sync authorization failed for %s on %s: %s",
            serial_number,
            olt_id,
            exc,
            exc_info=True,
        )
        db.rollback()
        return AuthorizationExecutionResult(
            success=False,
            message=f"Authorization failed: {exc}",
            mode=AuthorizationMode.sync_immediate,
            serial_number=serial_number,
            fsp=fsp,
            error=str(exc),
        )


def execute_authorization_batch(
    db: Session,
    authorizations: list[dict],
    *,
    force_reauthorize: bool = False,
    initiated_by: str | None = None,
    request: Request | None = None,
    prefer_sync: bool = False,
) -> list[AuthorizationExecutionResult]:
    """Execute multiple authorizations.

    Args:
        db: Database session
        authorizations: List of dicts with keys: olt_id, fsp, serial_number
        force_reauthorize: If True, delete existing registrations first
        initiated_by: User/system that initiated the action
        request: HTTP request for audit logging
        prefer_sync: If True, run all synchronously

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
            prefer_sync=prefer_sync,
        )
        results.append(result)
    return results
