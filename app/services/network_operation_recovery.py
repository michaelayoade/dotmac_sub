"""Reviewed, idempotent recovery for failed network operations.

The operation ledger owns retry eligibility and immutable attempt lineage.
Handlers are explicit and typed; an unregistered operation fails closed rather
than redispatching an arbitrary Celery task or replaying an old payload.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models.network import OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network_operation_dispatch import (
    NetworkOperationCommand,
    NetworkOperationDispatchError,
    stage_dispatch,
)
from app.services.network_operations import network_operations
from app.services.observability import record_metric

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

REDRIVE_PERMISSION = "network:operation:redrive"
_ACTIVE_STATUSES = (
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
    NetworkOperationStatus.waiting,
)


class RedriveOutcome(StrEnum):
    queued = "queued"
    replayed = "replayed"


class NetworkOperationRecoveryError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class RedriveReview:
    operation_id: str
    eligible: bool
    code: str
    message: str
    expected_head: str | None = None
    action_label: str | None = None
    required_permission: str = REDRIVE_PERMISSION


@dataclass(frozen=True)
class RedriveExecutionResult:
    operation: NetworkOperation
    outcome: RedriveOutcome
    replayed: bool
    message: str


@dataclass(frozen=True)
class _RedriveHandler:
    key: str
    command: NetworkOperationCommand
    action_label: str
    matches: Callable[[NetworkOperation], bool]
    target_head: Callable[[Session, NetworkOperation], dict[str, object]]
    correlation_key: Callable[[NetworkOperation], str]
    input_payload: Callable[[NetworkOperation], dict[str, object]]


def _is_ont_status_refresh(operation: NetworkOperation) -> bool:
    payload = operation.input_payload or {}
    return (
        operation.operation_type == NetworkOperationType.olt_ont_sync
        and operation.target_type == NetworkOperationTargetType.ont
        and payload.get("action") == "status_refresh"
    )


def _ont_status_refresh_head(
    db: Session, operation: NetworkOperation
) -> dict[str, object]:
    ont = db.get(OntUnit, operation.target_id)
    if ont is None:
        raise NetworkOperationRecoveryError(
            "target_not_found",
            "The ONT no longer exists.",
            status_code=404,
        )
    return {
        "target_id": str(ont.id),
        "serial_number": ont.serial_number,
        "is_active": bool(ont.is_active),
        "olt_device_id": str(ont.olt_device_id) if ont.olt_device_id else None,
        "external_id": ont.external_id,
        "uisp_device_id": ont.uisp_device_id,
    }


def _ont_status_refresh_correlation(operation: NetworkOperation) -> str:
    return f"ont_status_refresh:{operation.target_id}"


def _ont_status_refresh_payload(
    _operation: NetworkOperation,
) -> dict[str, object]:
    return {"action": "status_refresh"}


_HANDLERS = (
    _RedriveHandler(
        key="ont_status_refresh",
        command=NetworkOperationCommand.ont_status_refresh_v1,
        action_label="Retry status refresh",
        matches=_is_ont_status_refresh,
        target_head=_ont_status_refresh_head,
        correlation_key=_ont_status_refresh_correlation,
        input_payload=_ont_status_refresh_payload,
    ),
)


def _handler_for(operation: NetworkOperation) -> _RedriveHandler | None:
    return next((handler for handler in _HANDLERS if handler.matches(operation)), None)


def _review_head(
    operation: NetworkOperation,
    *,
    handler: _RedriveHandler,
    target_head: dict[str, object],
) -> str:
    payload = {
        "contract_version": 1,
        "handler": handler.key,
        "operation": {
            "id": str(operation.id),
            "operation_type": operation.operation_type.value,
            "target_type": operation.target_type.value,
            "target_id": str(operation.target_id),
            "status": operation.status.value,
            "retry_count": int(operation.retry_count or 0),
            "max_retries": int(operation.max_retries or 0),
            "completed_at": operation.completed_at,
            "updated_at": operation.updated_at,
            "error": operation.error,
        },
        "target": target_head,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ineligible(
    operation: NetworkOperation,
    code: str,
    message: str,
) -> RedriveReview:
    return RedriveReview(
        operation_id=str(operation.id),
        eligible=False,
        code=code,
        message=message,
    )


def review_redrive(
    db: Session,
    operation: NetworkOperation,
) -> RedriveReview:
    """Resolve current retry eligibility without changing operation state."""
    if operation.status != NetworkOperationStatus.failed:
        return _ineligible(
            operation,
            "source_not_failed",
            "Only failed operations can be retried.",
        )
    if int(operation.retry_count or 0) >= int(operation.max_retries or 0):
        return _ineligible(
            operation,
            "retry_limit_reached",
            "The operation retry limit has been reached.",
        )

    handler = _handler_for(operation)
    if handler is None:
        return _ineligible(
            operation,
            "unsupported_operation",
            "This operation has no approved recovery handler.",
        )

    prior_attempt = db.scalars(
        select(NetworkOperation.id).where(
            NetworkOperation.redrive_of_id == operation.id
        )
    ).first()
    if prior_attempt is not None:
        return _ineligible(
            operation,
            "superseded_by_redrive",
            "A newer retry attempt already exists.",
        )

    correlation_key = handler.correlation_key(operation)
    active = db.scalars(
        select(NetworkOperation.id).where(
            NetworkOperation.id != operation.id,
            NetworkOperation.correlation_key == correlation_key,
            NetworkOperation.status.in_(_ACTIVE_STATUSES),
        )
    ).first()
    if active is not None:
        return _ineligible(
            operation,
            "operation_in_progress",
            "A current operation is already in progress for this target.",
        )

    try:
        target_head = handler.target_head(db, operation)
    except NetworkOperationRecoveryError as exc:
        return _ineligible(operation, exc.code, exc.message)
    return RedriveReview(
        operation_id=str(operation.id),
        eligible=True,
        code="eligible",
        message="The failed operation can be retried from current target state.",
        expected_head=_review_head(
            operation,
            handler=handler,
            target_head=target_head,
        ),
        action_label=handler.action_label,
    )


def get_redrive_review(db: Session, operation_id: str) -> RedriveReview:
    try:
        parsed_id = UUID(str(operation_id))
    except ValueError as exc:
        raise NetworkOperationRecoveryError(
            "operation_not_found", "Operation not found.", status_code=404
        ) from exc
    operation = db.get(NetworkOperation, parsed_id)
    if operation is None:
        raise NetworkOperationRecoveryError(
            "operation_not_found", "Operation not found.", status_code=404
        )
    return review_redrive(db, operation)


def _normalize_required(
    value: str,
    *,
    label: str,
    min_length: int,
    max_length: int,
) -> str:
    normalized = str(value or "").strip()
    if not min_length <= len(normalized) <= max_length:
        raise NetworkOperationRecoveryError(
            f"invalid_{label}",
            f"{label.replace('_', ' ').title()} must be between "
            f"{min_length} and {max_length} characters.",
            status_code=422,
        )
    return normalized


def _record_redrive_outcome(
    operation: NetworkOperation,
    outcome: str,
) -> None:
    record_metric(
        domain="network_operations",
        signal="redrive",
        status=outcome,
    )


def redrive_operation(
    db: Session,
    operation_id: str,
    *,
    expected_head: str,
    idempotency_key: str,
    reason: str,
    initiated_by: str,
) -> RedriveExecutionResult:
    """Create and dispatch one reviewed recovery attempt."""
    reviewed_head = _normalize_required(
        expected_head,
        label="expected_head",
        min_length=64,
        max_length=64,
    )
    key = _normalize_required(
        idempotency_key,
        label="idempotency_key",
        min_length=16,
        max_length=160,
    )
    normalized_reason = _normalize_required(
        reason,
        label="reason",
        min_length=8,
        max_length=500,
    )
    actor = _normalize_required(
        initiated_by,
        label="initiated_by",
        min_length=1,
        max_length=120,
    )
    try:
        parsed_id = UUID(str(operation_id))
    except ValueError as exc:
        raise NetworkOperationRecoveryError(
            "operation_not_found", "Operation not found.", status_code=404
        ) from exc

    source = db.scalar(
        select(NetworkOperation)
        .where(NetworkOperation.id == parsed_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is None:
        raise NetworkOperationRecoveryError(
            "operation_not_found", "Operation not found.", status_code=404
        )

    existing = db.scalars(
        select(NetworkOperation).where(
            NetworkOperation.redrive_of_id == source.id,
            NetworkOperation.redrive_idempotency_key == key,
        )
    ).first()
    if existing is not None:
        if (
            existing.redrive_reason != normalized_reason
            or existing.redrive_reviewed_head != reviewed_head
        ):
            _record_redrive_outcome(source, "idempotency_conflict")
            raise NetworkOperationRecoveryError(
                "idempotency_conflict",
                "The idempotency key was already used for a different retry request.",
            )
        handler = _handler_for(source)
        if handler is None:
            raise NetworkOperationRecoveryError(
                "unsupported_operation",
                "This operation has no approved recovery handler.",
            )
        _record_redrive_outcome(existing, RedriveOutcome.replayed.value)
        return RedriveExecutionResult(
            operation=existing,
            outcome=RedriveOutcome.replayed,
            replayed=True,
            message="The existing recovery attempt was returned.",
        )

    review = review_redrive(db, source)
    if not review.eligible or review.expected_head is None:
        _record_redrive_outcome(source, review.code)
        raise NetworkOperationRecoveryError(review.code, review.message)
    if review.expected_head != reviewed_head:
        _record_redrive_outcome(source, "stale_review")
        raise NetworkOperationRecoveryError(
            "stale_review",
            "Operation or target state changed after review. Reload before retrying.",
        )

    handler = _handler_for(source)
    if handler is None:  # Defensive: review already failed closed above.
        raise NetworkOperationRecoveryError(
            "unsupported_operation",
            "This operation has no approved recovery handler.",
        )
    payload = handler.input_payload(source)
    payload["_redrive"] = {
        "source_operation_id": str(source.id),
        "reason": normalized_reason,
        "reviewed_head": reviewed_head,
    }
    try:
        operation, replayed = network_operations.start_redrive(
            db,
            source,
            correlation_key=handler.correlation_key(source),
            input_payload=payload,
            reason=normalized_reason,
            reviewed_head=reviewed_head,
            idempotency_key=key,
            initiated_by=actor,
        )
        stage_dispatch(db, operation, handler.command)
    except HTTPException as exc:
        raise NetworkOperationRecoveryError(
            "operation_conflict",
            str(exc.detail),
            status_code=exc.status_code,
        ) from exc
    except NetworkOperationDispatchError as exc:
        db.rollback()
        raise NetworkOperationRecoveryError(exc.code, exc.message) from exc
    db.commit()

    _record_redrive_outcome(operation, RedriveOutcome.queued.value)
    return RedriveExecutionResult(
        operation=operation,
        outcome=RedriveOutcome.queued,
        replayed=replayed,
        message="Recovery operation queued.",
    )
