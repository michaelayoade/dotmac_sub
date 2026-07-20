"""Durable scheduling and retry for deferred subscription status commands."""

from __future__ import annotations

import logging
import socket
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.subscription_lifecycle_schedule import (
    SubscriptionLifecycleSchedule,
    SubscriptionLifecycleScheduleStatus,
)
from app.services.common import coerce_uuid
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecyclePreview,
)

logger = logging.getLogger(__name__)

_LEASE_DURATION = timedelta(minutes=15)
_BASE_RETRY_DELAY = timedelta(minutes=5)
_MAX_RETRY_DELAY = timedelta(hours=1)
_STATUS_COMMANDS = frozenset(
    {
        SubscriptionCommandKind.activate,
        SubscriptionCommandKind.suspend,
        SubscriptionCommandKind.restore,
        SubscriptionCommandKind.cancel,
        SubscriptionCommandKind.expire,
    }
)


class SubscriptionLifecycleScheduleError(ValueError):
    pass


def schedule_subscription_status_command(
    db: Session,
    command: SubscriptionLifecycleCommand,
    preview: SubscriptionLifecyclePreview,
    *,
    actor_id: str | None,
    actor_type: AuditActorType,
) -> SubscriptionLifecycleSchedule:
    """Persist one already-reviewed deferred status command."""
    if command.kind not in _STATUS_COMMANDS:
        raise SubscriptionLifecycleScheduleError(
            f"{command.kind.value} is not owned by the status scheduler"
        )
    if command.effective_timing == SubscriptionEffectiveTiming.immediate:
        raise SubscriptionLifecycleScheduleError(
            "Immediate commands cannot be added to the lifecycle scheduler"
        )

    from app.services.subscription_lifecycle_commands import (
        subscription_command_fingerprint,
        subscription_command_idempotency_key,
    )

    effective_at = _aware_utc(preview.effective_at) or datetime.now(UTC)
    schedule = SubscriptionLifecycleSchedule(
        subscription_id=coerce_uuid(command.subscription_id),
        command_kind=command.kind.value,
        source=command.source,
        effective_timing=command.effective_timing.value,
        effective_at=effective_at,
        reason=command.reason,
        reviewed_head=preview.current.head,
        command_fingerprint=subscription_command_fingerprint(command),
        idempotency_key=subscription_command_idempotency_key(command),
        actor_id=actor_id,
        actor_type=actor_type.value,
        status=SubscriptionLifecycleScheduleStatus.pending,
        next_attempt_at=effective_at,
    )
    db.add(schedule)
    db.flush()
    return schedule


def cancel_scheduled_subscription_status_command(
    db: Session,
    schedule_id: str,
    *,
    subscription_id: str,
    actor_id: str | None,
    now: datetime | None = None,
) -> SubscriptionLifecycleSchedule:
    """Cancel a pending schedule before any worker has claimed it."""
    schedule = db.scalar(
        select(SubscriptionLifecycleSchedule)
        .where(SubscriptionLifecycleSchedule.id == coerce_uuid(schedule_id))
        .where(
            SubscriptionLifecycleSchedule.subscription_id
            == coerce_uuid(subscription_id)
        )
        .with_for_update()
    )
    if schedule is None:
        raise SubscriptionLifecycleScheduleError("Lifecycle schedule not found")
    if schedule.status == SubscriptionLifecycleScheduleStatus.canceled:
        return schedule
    if schedule.status != SubscriptionLifecycleScheduleStatus.pending:
        raise SubscriptionLifecycleScheduleError(
            f"Lifecycle schedule cannot be canceled from {schedule.status.value}"
        )
    canceled_at = _aware_utc(now) or datetime.now(UTC)
    schedule.status = SubscriptionLifecycleScheduleStatus.canceled
    schedule.canceled_at = canceled_at
    schedule.canceled_by = actor_id
    schedule.claimed_at = None
    schedule.claim_expires_at = None
    schedule.claimed_by = None
    db.commit()
    return schedule


def apply_due_subscription_status_commands(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
    worker_id: str | None = None,
) -> dict[str, int]:
    """Apply due status schedules with lease recovery and bounded retry."""
    effective_now = _aware_utc(now) or datetime.now(UTC)
    worker = worker_id or socket.gethostname()
    counts = {
        "claimed": 0,
        "applied": 0,
        "retried": 0,
        "superseded": 0,
        "rejected": 0,
        "failed": 0,
    }
    for _ in range(max(0, min(limit, 1000))):
        schedule = _claim_due_schedule(db, now=effective_now, worker_id=worker)
        if schedule is None:
            break
        counts["claimed"] += 1
        result = _execute_claimed_schedule(db, schedule.id, now=effective_now)
        counts[result] += 1
    return counts


def _claim_due_schedule(
    db: Session,
    *,
    now: datetime,
    worker_id: str,
) -> SubscriptionLifecycleSchedule | None:
    due_pending = and_(
        SubscriptionLifecycleSchedule.status
        == SubscriptionLifecycleScheduleStatus.pending,
        SubscriptionLifecycleSchedule.next_attempt_at <= now,
    )
    expired_lease = and_(
        SubscriptionLifecycleSchedule.status
        == SubscriptionLifecycleScheduleStatus.processing,
        SubscriptionLifecycleSchedule.claim_expires_at.isnot(None),
        SubscriptionLifecycleSchedule.claim_expires_at <= now,
    )
    schedule = db.scalar(
        select(SubscriptionLifecycleSchedule)
        .where(SubscriptionLifecycleSchedule.effective_at <= now)
        .where(or_(due_pending, expired_lease))
        .order_by(
            SubscriptionLifecycleSchedule.next_attempt_at.asc(),
            SubscriptionLifecycleSchedule.created_at.asc(),
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if schedule is None:
        return None
    schedule.status = SubscriptionLifecycleScheduleStatus.processing
    schedule.attempt_count += 1
    schedule.claimed_at = now
    schedule.claim_expires_at = now + _LEASE_DURATION
    schedule.claimed_by = worker_id
    db.commit()
    return schedule


def _execute_claimed_schedule(
    db: Session,
    schedule_id: object,
    *,
    now: datetime,
) -> str:
    schedule = db.get(SubscriptionLifecycleSchedule, schedule_id)
    if schedule is None:  # pragma: no cover - protected by FK and claim
        return "failed"
    persisted_schedule_id = schedule.id
    command = SubscriptionLifecycleCommand(
        subscription_id=str(schedule.subscription_id),
        kind=SubscriptionCommandKind(schedule.command_kind),
        source=schedule.source,
        effective_timing=SubscriptionEffectiveTiming.immediate,
        reason=schedule.reason,
        expected_head=schedule.reviewed_head,
        idempotency_key=f"lifecycle-schedule:{schedule.id}",
    )
    try:
        from app.services.subscription_lifecycle_commands import (
            execute_subscription_command,
        )

        outcome = execute_subscription_command(
            db,
            command,
            actor_id=schedule.actor_id,
            actor_type=_actor_type(schedule.actor_type),
            now=now,
        )
    except Exception as exc:
        logger.exception("Deferred lifecycle command execution failed: %s", schedule.id)
        db.rollback()
        return _record_retry_or_failure(
            db,
            persisted_schedule_id,
            now=now,
            error_code="scheduled_command_execution_failed",
            message=str(exc) or exc.__class__.__name__,
        )

    locked = db.scalar(
        select(SubscriptionLifecycleSchedule)
        .where(SubscriptionLifecycleSchedule.id == schedule.id)
        .with_for_update()
    )
    if locked is None:  # pragma: no cover - protected by FK and claim
        return "failed"
    locked.last_error_code = outcome.error_code
    locked.last_message = outcome.message
    locked.outcome_head = outcome.current_head
    locked.artifact_ids = list(outcome.artifact_ids)
    _clear_claim(locked)
    if outcome.status in {
        SubscriptionCommandOutcomeStatus.applied,
        SubscriptionCommandOutcomeStatus.skipped,
    }:
        locked.status = SubscriptionLifecycleScheduleStatus.applied
        locked.applied_at = now
        result = "applied"
    elif outcome.status == SubscriptionCommandOutcomeStatus.superseded:
        locked.status = SubscriptionLifecycleScheduleStatus.superseded
        result = "superseded"
    elif outcome.status == SubscriptionCommandOutcomeStatus.rejected:
        locked.status = SubscriptionLifecycleScheduleStatus.rejected
        result = "rejected"
    else:
        return _retry_or_fail_locked(db, locked, now=now)
    db.commit()
    return result


def _record_retry_or_failure(
    db: Session,
    schedule_id: object,
    *,
    now: datetime,
    error_code: str,
    message: str,
) -> str:
    schedule = db.scalar(
        select(SubscriptionLifecycleSchedule)
        .where(SubscriptionLifecycleSchedule.id == schedule_id)
        .with_for_update()
    )
    if schedule is None:
        return "failed"
    schedule.last_error_code = error_code
    schedule.last_message = message
    return _retry_or_fail_locked(db, schedule, now=now)


def _retry_or_fail_locked(
    db: Session,
    schedule: SubscriptionLifecycleSchedule,
    *,
    now: datetime,
) -> str:
    _clear_claim(schedule)
    if schedule.attempt_count >= schedule.max_attempts:
        schedule.status = SubscriptionLifecycleScheduleStatus.failed
        result = "failed"
    else:
        multiplier = 2 ** max(0, schedule.attempt_count - 1)
        delay = min(_BASE_RETRY_DELAY * multiplier, _MAX_RETRY_DELAY)
        schedule.status = SubscriptionLifecycleScheduleStatus.pending
        schedule.next_attempt_at = now + delay
        result = "retried"
    schedule.updated_at = now
    db.commit()
    return result


def _clear_claim(schedule: SubscriptionLifecycleSchedule) -> None:
    schedule.claimed_at = None
    schedule.claim_expires_at = None
    schedule.claimed_by = None


def _actor_type(value: str) -> AuditActorType:
    try:
        return AuditActorType(value)
    except ValueError:
        return AuditActorType.system


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SubscriptionLifecycleScheduleError",
    "apply_due_subscription_status_commands",
    "cancel_scheduled_subscription_status_command",
    "schedule_subscription_status_command",
]
