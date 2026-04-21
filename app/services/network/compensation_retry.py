"""Compensation failure retry service.

Provides operations for listing, retrying, and resolving failed compensation entries.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.compensation_failure import CompensationFailure, CompensationStatus
from app.models.network import OLTDevice
from app.services.network.olt_ssh_session import CliMode, olt_session

logger = logging.getLogger(__name__)

_DEFAULT_RETRY_BASE_SECONDS = 300
_DEFAULT_RETRY_MAX_SECONDS = 21600
_SERVICE_LAYER_RETRY_STEPS = {"rollback_service_ports"}


def retry_backoff_seconds(
    failure_count: int,
    *,
    base_seconds: int = _DEFAULT_RETRY_BASE_SECONDS,
    max_seconds: int = _DEFAULT_RETRY_MAX_SECONDS,
) -> int:
    """Return exponential backoff delay for a compensation retry attempt."""
    effective_count = max(1, int(failure_count or 1))
    exponent = max(0, effective_count - 1)
    delay = base_seconds * (2**exponent)
    return min(max(base_seconds, delay), max_seconds)


def next_retry_at(
    failure: CompensationFailure,
    *,
    base_seconds: int = _DEFAULT_RETRY_BASE_SECONDS,
    max_seconds: int = _DEFAULT_RETRY_MAX_SECONDS,
) -> datetime:
    """Return when a pending compensation becomes eligible for retry."""
    attempted_at = failure.last_attempted_at
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=UTC)
    return attempted_at + timedelta(
        seconds=retry_backoff_seconds(
            failure.failure_count,
            base_seconds=base_seconds,
            max_seconds=max_seconds,
        )
    )


def is_retry_due(
    failure: CompensationFailure,
    *,
    now: datetime | None = None,
    base_seconds: int = _DEFAULT_RETRY_BASE_SECONDS,
    max_seconds: int = _DEFAULT_RETRY_MAX_SECONDS,
) -> bool:
    """Return whether the compensation failure is ready for another retry."""
    if failure.status != CompensationStatus.pending:
        return False
    current_time = now or datetime.now(UTC)
    return next_retry_at(
        failure,
        base_seconds=base_seconds,
        max_seconds=max_seconds,
    ) <= current_time


def list_pending_compensations(
    db: Session,
    *,
    olt_id: str | UUID | None = None,
    ont_id: str | UUID | None = None,
    limit: int = 100,
) -> list[CompensationFailure]:
    """List pending compensation failures.

    Args:
        db: Database session.
        olt_id: Optional filter by OLT device ID.
        ont_id: Optional filter by ONT unit ID.
        limit: Maximum number of records to return.

    Returns:
        List of pending CompensationFailure records.
    """
    stmt = (
        select(CompensationFailure)
        .where(CompensationFailure.status == CompensationStatus.pending)
        .order_by(CompensationFailure.created_at.desc())
        .limit(limit)
    )

    if olt_id is not None:
        stmt = stmt.where(CompensationFailure.olt_device_id == str(olt_id))
    if ont_id is not None:
        stmt = stmt.where(CompensationFailure.ont_unit_id == str(ont_id))

    return list(db.scalars(stmt).all())


def list_retry_due_compensations(
    db: Session,
    *,
    limit: int = 50,
    now: datetime | None = None,
    base_seconds: int = _DEFAULT_RETRY_BASE_SECONDS,
    max_seconds: int = _DEFAULT_RETRY_MAX_SECONDS,
) -> list[CompensationFailure]:
    """List pending compensation failures that are due for retry now."""
    current_time = now or datetime.now(UTC)
    stmt = (
        select(CompensationFailure)
        .where(CompensationFailure.status == CompensationStatus.pending)
        .order_by(CompensationFailure.last_attempted_at.asc())
        .limit(limit)
    )
    candidates = list(db.scalars(stmt).all())
    return [
        failure
        for failure in candidates
        if is_retry_due(
            failure,
            now=current_time,
            base_seconds=base_seconds,
            max_seconds=max_seconds,
        )
    ]


def retry_compensation(
    db: Session,
    failure_id: str | UUID,
    *,
    resolved_by: str | None = None,
) -> tuple[bool, str]:
    """Retry a failed compensation entry.

    Executes the undo commands on the OLT. If successful, marks the failure
    as resolved. If failed, increments the failure count.

    Args:
        db: Database session.
        failure_id: ID of the CompensationFailure to retry.
        resolved_by: Optional username/actor who initiated the retry.

    Returns:
        Tuple of (success, message).
    """
    failure = db.get(CompensationFailure, failure_id)
    if failure is None:
        return False, "Compensation failure not found"

    if failure.status != CompensationStatus.pending:
        return False, f"Cannot retry: status is {failure.status.value}"

    if not failure.undo_commands and failure.step_name in _SERVICE_LAYER_RETRY_STEPS:
        return _retry_service_layer_compensation(
            db,
            failure,
            resolved_by=resolved_by,
        )

    olt = db.get(OLTDevice, failure.olt_device_id)
    if olt is None:
        return False, "OLT device not found"

    # Execute the compensation commands
    try:
        with olt_session(olt) as session:
            # Enter interface mode if needed
            if failure.interface_path:
                session.run_command(
                    f"interface gpon {failure.interface_path}",
                    require_mode=CliMode.CONFIG,
                )

            # Execute each undo command
            all_success = True
            error_messages = []
            for cmd in failure.undo_commands:
                result = session.run_command(cmd)
                if not (result.success or result.is_idempotent_success):
                    all_success = False
                    error_messages.append(f"{cmd}: {result.message}")
                    logger.warning(
                        "Compensation retry command failed: %s -> %s",
                        cmd,
                        result.message,
                    )

            # Exit interface mode if we entered it
            if failure.interface_path:
                session.run_command("quit")

            if all_success:
                # Mark as resolved
                failure.status = CompensationStatus.resolved
                failure.resolved_at = datetime.now(UTC)
                failure.resolved_by = resolved_by
                failure.resolution_notes = "Successfully retried"
                db.flush()
                logger.info(
                    "Compensation failure %s resolved via retry",
                    failure_id,
                )
                return True, "Compensation commands executed successfully"
            else:
                # Update failure count and last attempt
                failure.failure_count += 1
                failure.last_attempted_at = datetime.now(UTC)
                failure.error_message = "; ".join(error_messages)
                db.flush()
                return False, f"Retry failed: {'; '.join(error_messages)}"

    except Exception as exc:
        logger.error(
            "Compensation retry failed for %s: %s",
            failure_id,
            exc,
            extra={"event": "compensation_retry_error"},
        )
        # Update failure count
        failure.failure_count += 1
        failure.last_attempted_at = datetime.now(UTC)
        failure.error_message = str(exc)
        db.flush()
        return False, f"Retry error: {exc}"


def _retry_service_layer_compensation(
    db: Session,
    failure: CompensationFailure,
    *,
    resolved_by: str | None = None,
) -> tuple[bool, str]:
    """Retry compensation through a domain service when no raw undo commands exist."""
    from app.services.network import ont_provision_steps

    if failure.ont_unit_id is None:
        return False, "ONT unit not found for compensation retry"

    if failure.step_name == "rollback_service_ports":
        result = ont_provision_steps.rollback_service_ports(db, str(failure.ont_unit_id))
        if result.success:
            failure.status = CompensationStatus.resolved
            failure.resolved_at = datetime.now(UTC)
            failure.resolved_by = resolved_by
            failure.resolution_notes = "Successfully retried via service layer"
            db.flush()
            return True, result.message

        failure.failure_count += 1
        failure.last_attempted_at = datetime.now(UTC)
        failure.error_message = result.message
        db.flush()
        return False, result.message

    return False, f"No service-layer retry handler for step {failure.step_name}"


def mark_abandoned(
    db: Session,
    failure_id: str | UUID,
    *,
    resolved_by: str | None = None,
    notes: str | None = None,
) -> tuple[bool, str]:
    """Mark a compensation failure as abandoned.

    Use this when the failure cannot or should not be retried, e.g., the
    resource was manually cleaned up or the issue was resolved another way.

    Args:
        db: Database session.
        failure_id: ID of the CompensationFailure to abandon.
        resolved_by: Optional username/actor who marked it abandoned.
        notes: Optional resolution notes explaining why it was abandoned.

    Returns:
        Tuple of (success, message).
    """
    failure = db.get(CompensationFailure, failure_id)
    if failure is None:
        return False, "Compensation failure not found"

    if failure.status != CompensationStatus.pending:
        return False, f"Cannot abandon: status is {failure.status.value}"

    failure.status = CompensationStatus.abandoned
    failure.resolved_at = datetime.now(UTC)
    failure.resolved_by = resolved_by
    failure.resolution_notes = notes or "Marked as abandoned"
    db.flush()

    logger.info(
        "Compensation failure %s marked as abandoned by %s",
        failure_id,
        resolved_by or "unknown",
    )
    return True, "Compensation failure marked as abandoned"


def mark_resolved(
    db: Session,
    failure_id: str | UUID,
    *,
    resolved_by: str | None = None,
    notes: str | None = None,
) -> tuple[bool, str]:
    """Mark a compensation failure as resolved manually.

    Use this when the issue was resolved manually outside of this system.

    Args:
        db: Database session.
        failure_id: ID of the CompensationFailure to resolve.
        resolved_by: Optional username/actor who resolved it.
        notes: Optional resolution notes.

    Returns:
        Tuple of (success, message).
    """
    failure = db.get(CompensationFailure, failure_id)
    if failure is None:
        return False, "Compensation failure not found"

    if failure.status != CompensationStatus.pending:
        return False, f"Cannot resolve: status is {failure.status.value}"

    failure.status = CompensationStatus.resolved
    failure.resolved_at = datetime.now(UTC)
    failure.resolved_by = resolved_by
    failure.resolution_notes = notes or "Manually resolved"
    db.flush()

    logger.info(
        "Compensation failure %s manually resolved by %s",
        failure_id,
        resolved_by or "unknown",
    )
    return True, "Compensation failure marked as resolved"


def retry_due_compensations(
    db: Session,
    *,
    limit: int = 20,
    resolved_by: str = "system:watchdog",
    now: datetime | None = None,
    base_seconds: int = _DEFAULT_RETRY_BASE_SECONDS,
    max_seconds: int = _DEFAULT_RETRY_MAX_SECONDS,
) -> dict[str, object]:
    """Retry all due pending compensations and return an execution summary."""
    current_time = now or datetime.now(UTC)
    due_failures = list_retry_due_compensations(
        db,
        limit=limit,
        now=current_time,
        base_seconds=base_seconds,
        max_seconds=max_seconds,
    )
    summary: dict[str, object] = {
        "checked_at": current_time.isoformat(),
        "due_count": len(due_failures),
        "retried": 0,
        "resolved": 0,
        "still_pending": 0,
        "errors": [],
    }
    errors: list[dict[str, str]] = []

    for failure in due_failures:
        success, message = retry_compensation(
            db,
            failure.id,
            resolved_by=resolved_by,
        )
        summary["retried"] = int(summary["retried"]) + 1
        refreshed = db.get(CompensationFailure, failure.id)
        if success and refreshed is not None:
            summary["resolved"] = int(summary["resolved"]) + 1
            continue
        summary["still_pending"] = int(summary["still_pending"]) + 1
        errors.append({"failure_id": str(failure.id), "message": message})

    summary["errors"] = errors
    return summary
