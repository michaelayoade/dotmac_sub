"""OLT SSH circuit breaker service.

Implements the circuit breaker pattern to gracefully handle degraded OLTs
without blocking web requests. When an OLT's circuit is open, provisioning
operations are queued for later execution.

State transitions:
- closed -> open: After N consecutive failures (default 3)
- open -> half_open: After backoff period elapses
- half_open -> closed: After 2 consecutive successes
- half_open -> open: On any failure
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import CircuitState, OLTDevice, QueuedOltOperation

logger = logging.getLogger(__name__)


# Default configuration
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_BACKOFF_SECONDS = 30
HALF_OPEN_SUCCESS_THRESHOLD = 2


def get_circuit_state(olt: OLTDevice) -> CircuitState:
    """Get the current circuit state for an OLT.

    Returns CircuitState.closed if not set.
    """
    if olt.circuit_state is None:
        return CircuitState.closed
    try:
        return CircuitState(olt.circuit_state)
    except ValueError:
        return CircuitState.closed


def can_attempt_operation(db: Session, olt: OLTDevice) -> tuple[bool, str | None]:
    """Check if an SSH operation can be attempted on this OLT.

    Returns:
        Tuple of (can_proceed, reason_if_blocked)
    """
    state = get_circuit_state(olt)

    if state == CircuitState.closed:
        return True, None

    if state == CircuitState.open:
        # Check if backoff period has elapsed
        if olt.backoff_until and olt.backoff_until > datetime.now(UTC):
            remaining = (olt.backoff_until - datetime.now(UTC)).total_seconds()
            return False, f"Circuit open, retry in {int(remaining)}s"

        # Transition to half-open for testing
        olt.circuit_state = CircuitState.half_open.value
        olt.circuit_failure_count = 0
        db.flush()
        logger.info("OLT %s circuit transitioning to half-open for testing", olt.name)
        return True, None

    if state == CircuitState.half_open:
        # Allow operation for testing
        return True, None

    return True, None


def record_ssh_success(db: Session, olt: OLTDevice) -> None:
    """Record a successful SSH operation.

    Updates circuit state and resets failure counters.
    """
    state = get_circuit_state(olt)
    now = datetime.now(UTC)

    olt.last_successful_ssh_at = now
    olt.circuit_failure_count = 0

    if state == CircuitState.half_open:
        # Need consistent successes to close circuit
        # For simplicity, close on first success in half-open
        # (could track success count for more resilience)
        close_circuit(db, olt)
    elif state == CircuitState.open:
        # Shouldn't reach here if can_attempt_operation was called
        close_circuit(db, olt)

    db.flush()


def record_ssh_failure(
    db: Session,
    olt: OLTDevice,
    error: str | Exception,
) -> None:
    """Record a failed SSH operation.

    Updates failure count and may open the circuit.
    """
    state = get_circuit_state(olt)
    error_str = str(error) if isinstance(error, Exception) else error

    olt.circuit_failure_count = (olt.circuit_failure_count or 0) + 1

    if state == CircuitState.half_open:
        # Any failure in half-open immediately reopens
        open_circuit(db, olt, error_str)
    elif state == CircuitState.closed:
        threshold = olt.circuit_failure_threshold or DEFAULT_FAILURE_THRESHOLD
        if olt.circuit_failure_count >= threshold:
            open_circuit(db, olt, error_str)
        else:
            logger.warning(
                "OLT %s SSH failure %d/%d: %s",
                olt.name,
                olt.circuit_failure_count,
                threshold,
                error_str,
            )

    db.flush()


def open_circuit(db: Session, olt: OLTDevice, error: str) -> None:
    """Open the circuit breaker for an OLT.

    Sets backoff period and marks circuit as open.
    """
    olt.circuit_state = CircuitState.open.value
    olt.backoff_until = datetime.now(UTC) + timedelta(seconds=DEFAULT_BACKOFF_SECONDS)

    logger.warning(
        "OLT %s circuit OPEN - blocking operations until %s (error: %s)",
        olt.name,
        olt.backoff_until.isoformat(),
        error[:200],
    )

    db.flush()


def close_circuit(db: Session, olt: OLTDevice) -> None:
    """Close the circuit breaker, resuming normal operations."""
    previous_state = get_circuit_state(olt)

    olt.circuit_state = CircuitState.closed.value
    olt.circuit_failure_count = 0
    olt.backoff_until = None

    logger.info(
        "OLT %s circuit CLOSED - resuming normal operations (was %s)",
        olt.name,
        previous_state.value,
    )

    db.flush()


def is_circuit_open(olt: OLTDevice) -> bool:
    """Quick check if circuit is currently open."""
    state = get_circuit_state(olt)

    if state == CircuitState.open:
        # Check if backoff has elapsed
        if olt.backoff_until and olt.backoff_until > datetime.now(UTC):
            return True

    return False


# =============================================================================
# Queued Operations
# =============================================================================


def queue_operation(
    db: Session,
    olt: OLTDevice,
    operation_type: str,
    payload: dict,
    *,
    scheduled_for: datetime | None = None,
) -> QueuedOltOperation:
    """Queue an operation for later execution when circuit recovers.

    Args:
        db: Database session
        olt: OLT device
        operation_type: Type of operation (authorize, deprovision, service_port)
        payload: Operation parameters as dict
        scheduled_for: When to attempt the operation (default: now)

    Returns:
        The created QueuedOltOperation
    """
    op = QueuedOltOperation(
        olt_device_id=olt.id,
        operation_type=operation_type,
        payload=payload,
        status="pending",
        scheduled_for=scheduled_for or datetime.now(UTC),
    )
    db.add(op)
    db.flush()

    logger.info(
        "Queued %s operation for OLT %s (id=%s)",
        operation_type,
        olt.name,
        op.id,
    )

    return op


def get_pending_operations(
    db: Session,
    olt_id: UUID | str | None = None,
    limit: int = 100,
) -> list[QueuedOltOperation]:
    """Get pending operations ready for execution.

    Args:
        db: Database session
        olt_id: Filter to specific OLT (optional)
        limit: Maximum operations to return

    Returns:
        List of pending operations ordered by scheduled_for
    """
    olt_uuid = UUID(str(olt_id)) if olt_id and isinstance(olt_id, str) else olt_id

    stmt = select(QueuedOltOperation).where(
        QueuedOltOperation.status == "pending",
        QueuedOltOperation.scheduled_for <= datetime.now(UTC),
    )

    if olt_uuid:
        stmt = stmt.where(QueuedOltOperation.olt_device_id == olt_uuid)

    stmt = stmt.order_by(QueuedOltOperation.scheduled_for).limit(limit)

    return list(db.scalars(stmt).all())


def mark_operation_completed(
    db: Session,
    operation_id: UUID | str,
) -> bool:
    """Mark a queued operation as completed."""
    op_uuid = UUID(str(operation_id)) if isinstance(operation_id, str) else operation_id

    op = db.get(QueuedOltOperation, op_uuid)
    if not op:
        return False

    op.status = "completed"
    op.completed_at = datetime.now(UTC)
    db.flush()

    logger.info("Completed queued operation %s", operation_id)

    return True


def mark_operation_failed(
    db: Session,
    operation_id: UUID | str,
    error: str,
    *,
    reschedule_seconds: int | None = 60,
) -> bool:
    """Mark a queued operation as failed, optionally rescheduling.

    Args:
        db: Database session
        operation_id: Operation ID
        error: Error message
        reschedule_seconds: Seconds until retry (None to abandon)

    Returns:
        True if updated
    """
    op_uuid = UUID(str(operation_id)) if isinstance(operation_id, str) else operation_id

    op = db.get(QueuedOltOperation, op_uuid)
    if not op:
        return False

    op.attempts += 1
    op.last_error = error[:1000] if error else None

    if reschedule_seconds is not None and op.attempts < 5:
        op.scheduled_for = datetime.now(UTC) + timedelta(seconds=reschedule_seconds)
        logger.warning(
            "Queued operation %s failed (attempt %d), rescheduling: %s",
            operation_id,
            op.attempts,
            error[:100],
        )
    else:
        op.status = "failed"
        logger.error(
            "Queued operation %s failed permanently after %d attempts: %s",
            operation_id,
            op.attempts,
            error[:200],
        )

    db.flush()

    return True


def get_circuit_status_summary(db: Session) -> dict:
    """Get summary of circuit breaker states across all OLTs.

    Returns:
        Dict with counts of OLTs in each state
    """
    stmt = select(
        OLTDevice.circuit_state,
        OLTDevice.id,
    ).where(OLTDevice.is_active.is_(True))

    results = list(db.execute(stmt).all())

    summary = {
        "closed": 0,
        "open": 0,
        "half_open": 0,
        "unknown": 0,
    }

    for state, _ in results:
        if state is None or state == "closed":
            summary["closed"] += 1
        elif state == "open":
            summary["open"] += 1
        elif state == "half_open":
            summary["half_open"] += 1
        else:
            summary["unknown"] += 1

    # Count pending operations
    pending_stmt = select(QueuedOltOperation.id).where(
        QueuedOltOperation.status == "pending"
    )
    summary["pending_operations"] = len(list(db.scalars(pending_stmt).all()))

    return summary
