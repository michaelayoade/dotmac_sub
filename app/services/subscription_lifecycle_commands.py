"""Canonical execution boundary for one subscription lifecycle command.

This service owns orchestration, locking, idempotent replay, and structured
outcomes. Account lifecycle, catalog, billing, scheduling, and RADIUS services
remain the owners of their respective mutations and side effects.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.idempotency import IdempotencyKey
from app.services.audit_adapter import record_audit_event
from app.services.common import coerce_uuid
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcome,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecycleError,
    SubscriptionLifecycleHeadConflict,
    SubscriptionLifecyclePreview,
    preview_subscription_command,
    resolve_subscription_lifecycle,
)

logger = logging.getLogger(__name__)

_IDEMPOTENCY_SCOPE = "subscription_lifecycle"


class SubscriptionCommandExecutionRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def execute_subscription_command(
    db: Session,
    command: SubscriptionLifecycleCommand,
    *,
    actor_id: str | None = None,
    actor_type: AuditActorType = AuditActorType.system,
    now: datetime | None = None,
) -> SubscriptionCommandOutcome:
    """Validate and execute one reviewed lifecycle command.

    The subscription row lock serializes review-head validation, idempotency
    reservation, and dispatch for a subscription. Replays are resolved before
    validating ``expected_head`` because a successfully applied first request
    necessarily changes that head.
    """
    effective_now = _aware_utc(now) or datetime.now(UTC)
    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == coerce_uuid(command.subscription_id))
        .with_for_update()
    )
    if subscription is None:
        raise SubscriptionLifecycleError(
            f"Subscription {command.subscription_id} not found"
        )

    current = resolve_subscription_lifecycle(db, command.subscription_id)
    prior = _find_idempotency_reservation(db, command)
    if prior is not None:
        outcome = _replay_outcome(db, command, current_head=current.head, prior=prior)
        if not outcome.replayed:
            _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    try:
        preview = preview_subscription_command(db, command, now=effective_now)
    except SubscriptionLifecycleHeadConflict as exc:
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.superseded,
            message=str(exc),
            previous_head=current.head,
            current_head=current.head,
            error_code="subscription_head_changed",
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    rejection = _execution_rejection(command, preview, now=effective_now)
    if rejection is not None:
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message=rejection[1],
            previous_head=current.head,
            current_head=current.head,
            error_code=rejection[0],
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    _reserve_idempotency(db, subscription, command)
    try:
        status, artifact_ids, message = _dispatch_command(
            db,
            subscription,
            command,
            preview,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        # Account lifecycle commands intentionally flush so callers can compose
        # them. Catalog and scheduler owners currently commit internally; this is
        # harmless in those paths and completes the transaction in the former.
        db.commit()
    except SubscriptionCommandExecutionRejected as exc:
        db.rollback()
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message=str(exc),
            previous_head=current.head,
            current_head=current.head,
            error_code=exc.code,
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome
    except Exception as exc:
        db.rollback()
        logger.exception(
            "Subscription lifecycle command failed: subscription=%s kind=%s",
            command.subscription_id,
            command.kind.value,
        )
        outcome = SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.failed,
            message=_exception_message(exc),
            previous_head=current.head,
            current_head=current.head,
            error_code="command_execution_failed",
        )
        _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
        return outcome

    updated = resolve_subscription_lifecycle(db, command.subscription_id)
    outcome = SubscriptionCommandOutcome(
        command=command,
        status=status,
        message=message,
        previous_head=current.head,
        current_head=updated.head,
        artifact_ids=artifact_ids,
    )
    _record_outcome(db, outcome, actor_id=actor_id, actor_type=actor_type)
    return outcome


def _execution_rejection(
    command: SubscriptionLifecycleCommand,
    preview: SubscriptionLifecyclePreview,
    *,
    now: datetime,
) -> tuple[str, str] | None:
    if not preview.eligible:
        reason = preview.eligibility_reasons[0]
        return reason, "Command is not eligible: " + ", ".join(
            preview.eligibility_reasons
        )
    if command.kind == SubscriptionCommandKind.renew:
        return (
            "renewal_execution_is_billing_owned",
            "Renewals must be executed by the billing cycle or payment settlement owner",
        )
    explicit_effective_at = _aware_utc(command.effective_at)
    if (
        command.effective_timing == SubscriptionEffectiveTiming.immediate
        and explicit_effective_at is not None
        and explicit_effective_at > now
    ):
        return (
            "future_effective_at_requires_scheduling",
            "A future effective time cannot be executed as an immediate command",
        )
    return None


def _dispatch_command(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    preview: SubscriptionLifecyclePreview,
    *,
    actor_id: str | None,
    actor_type: AuditActorType,
) -> tuple[SubscriptionCommandOutcomeStatus, tuple[str, ...], str]:
    if command.kind == SubscriptionCommandKind.change_plan:
        return _dispatch_plan_change(db, subscription, command, preview)
    if command.effective_timing != SubscriptionEffectiveTiming.immediate:
        from app.services.subscription_lifecycle_schedules import (
            schedule_subscription_status_command,
        )

        schedule = schedule_subscription_status_command(
            db,
            command,
            preview,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        return (
            SubscriptionCommandOutcomeStatus.scheduled,
            (str(schedule.id),),
            f"Subscription {command.kind.value} command scheduled for "
            f"{schedule.effective_at.isoformat()}",
        )

    from app.services.account_lifecycle import (
        suspend_subscription,
        transition_subscription_status,
    )

    reason = command.reason or f"{command.kind.value} lifecycle command"
    if command.kind == SubscriptionCommandKind.suspend:
        lock = suspend_subscription(
            db,
            command.subscription_id,
            reason=EnforcementReason.admin,
            source=command.source,
            notes=reason,
        )
        return (
            SubscriptionCommandOutcomeStatus.applied,
            (str(lock.id),),
            "Subscription suspension command applied",
        )

    target_status = {
        SubscriptionCommandKind.activate: SubscriptionStatus.active,
        SubscriptionCommandKind.restore: SubscriptionStatus.active,
        SubscriptionCommandKind.cancel: SubscriptionStatus.canceled,
        SubscriptionCommandKind.expire: SubscriptionStatus.expired,
    }.get(command.kind)
    if target_status is None:
        raise SubscriptionCommandExecutionRejected(
            "unsupported_subscription_command",
            f"Unsupported subscription command {command.kind.value}",
        )
    changed = transition_subscription_status(
        db,
        command.subscription_id,
        target_status,
        reason=reason,
        source=command.source,
    )
    if not changed:
        raise SubscriptionCommandExecutionRejected(
            f"{command.kind.value}_not_applied",
            f"Subscription {command.kind.value} command was not applied",
        )
    return (
        SubscriptionCommandOutcomeStatus.applied,
        (),
        f"Subscription {command.kind.value} command applied",
    )


def _dispatch_plan_change(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    preview: SubscriptionLifecyclePreview,
) -> tuple[SubscriptionCommandOutcomeStatus, tuple[str, ...], str]:
    target_offer_id = str(command.target_offer_id)
    if command.effective_timing == SubscriptionEffectiveTiming.immediate:
        from app.schemas.catalog import SubscriptionUpdate
        from app.services import catalog as catalog_service

        catalog_service.subscriptions.update(
            db,
            str(subscription.id),
            SubscriptionUpdate(offer_id=coerce_uuid(target_offer_id)),
            plan_change_operation_key=(
                command.idempotency_key or subscription_command_fingerprint(command)
            ),
        )
        return (
            SubscriptionCommandOutcomeStatus.applied,
            (),
            "Subscription plan changed",
        )

    from app.services.subscription_changes import subscription_change_requests

    request = subscription_change_requests.schedule(
        db,
        subscription_id=str(subscription.id),
        new_offer_id=target_offer_id,
        effective_date=preview.effective_at.date(),
        requested_by_person_id=None,
        notes=command.reason or f"Scheduled by {command.source}",
    )
    return (
        SubscriptionCommandOutcomeStatus.scheduled,
        (str(request.id),),
        f"Subscription plan change scheduled for {request.effective_date.isoformat()}",
    )


def _find_idempotency_reservation(
    db: Session,
    command: SubscriptionLifecycleCommand,
) -> IdempotencyKey | None:
    key = subscription_command_idempotency_key(command)
    if key is None:
        return None
    return db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
    )


def _reserve_idempotency(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
) -> None:
    key = subscription_command_idempotency_key(command)
    if key is None:
        return
    db.add(
        IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=subscription.subscriber_id,
            ref_id=subscription_command_fingerprint(command),
        )
    )
    db.flush()


def _replay_outcome(
    db: Session,
    command: SubscriptionLifecycleCommand,
    *,
    current_head: str,
    prior: IdempotencyKey,
) -> SubscriptionCommandOutcome:
    fingerprint = subscription_command_fingerprint(command)
    if prior.ref_id != fingerprint:
        return SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.rejected,
            message="Idempotency key was already used for a different command",
            previous_head=current_head,
            current_head=current_head,
            error_code="idempotency_key_conflict",
        )
    snapshot = resolve_subscription_lifecycle(db, command.subscription_id)
    artifact_ids: tuple[str, ...] = ()
    if (
        command.kind == SubscriptionCommandKind.change_plan
        and command.effective_timing != SubscriptionEffectiveTiming.immediate
        and snapshot.pending_change is not None
        and snapshot.pending_change.target_offer_id == str(command.target_offer_id)
    ):
        artifact_ids = (snapshot.pending_change.request_id,)
    elif command.effective_timing != SubscriptionEffectiveTiming.immediate:
        from app.models.subscription_lifecycle_schedule import (
            SubscriptionLifecycleSchedule,
        )

        schedule = db.scalar(
            select(SubscriptionLifecycleSchedule).where(
                SubscriptionLifecycleSchedule.subscription_id
                == coerce_uuid(command.subscription_id),
                SubscriptionLifecycleSchedule.idempotency_key
                == subscription_command_idempotency_key(command),
            )
        )
        if schedule is not None:
            artifact_ids = (str(schedule.id),)
    return SubscriptionCommandOutcome(
        command=command,
        status=SubscriptionCommandOutcomeStatus.skipped,
        message="Idempotent replay; command was not executed again",
        previous_head=command.expected_head or current_head,
        current_head=current_head,
        artifact_ids=artifact_ids,
        error_code="idempotent_replay",
        replayed=True,
    )


def subscription_command_idempotency_key(
    command: SubscriptionLifecycleCommand,
) -> str | None:
    if command.idempotency_key is None:
        return None
    source = f"{command.subscription_id}|{command.idempotency_key}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def subscription_command_fingerprint(command: SubscriptionLifecycleCommand) -> str:
    effective_at = _aware_utc(command.effective_at)
    payload = {
        "subscription_id": command.subscription_id,
        "kind": command.kind.value,
        "source": command.source,
        "effective_timing": command.effective_timing.value,
        "effective_at": effective_at.isoformat() if effective_at is not None else None,
        "target_offer_id": command.target_offer_id,
        "reason": command.reason,
        "expected_head": command.expected_head,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_outcome(
    db: Session,
    outcome: SubscriptionCommandOutcome,
    *,
    actor_id: str | None,
    actor_type: AuditActorType,
) -> None:
    try:
        record_audit_event(
            db,
            action=f"subscription_{outcome.command.kind.value}",
            entity_type="subscription_lifecycle_command",
            entity_id=outcome.command.subscription_id,
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=_audit_request_id(outcome.command.idempotency_key),
            is_success=outcome.status
            in {
                SubscriptionCommandOutcomeStatus.applied,
                SubscriptionCommandOutcomeStatus.scheduled,
                SubscriptionCommandOutcomeStatus.skipped,
            },
            status_code=(
                200
                if outcome.status
                in {
                    SubscriptionCommandOutcomeStatus.applied,
                    SubscriptionCommandOutcomeStatus.scheduled,
                    SubscriptionCommandOutcomeStatus.skipped,
                }
                else 409
                if outcome.status
                in {
                    SubscriptionCommandOutcomeStatus.rejected,
                    SubscriptionCommandOutcomeStatus.superseded,
                }
                else 500
            ),
            metadata={
                "kind": outcome.command.kind.value,
                "source": outcome.command.source,
                "status": outcome.status.value,
                "previous_head": outcome.previous_head,
                "current_head": outcome.current_head,
                "artifact_ids": list(outcome.artifact_ids),
                "error_code": outcome.error_code,
                "replayed": outcome.replayed,
            },
        )
    except Exception:
        db.rollback()
        logger.exception(
            "Failed to record lifecycle command audit: subscription=%s kind=%s",
            outcome.command.subscription_id,
            outcome.command.kind.value,
        )


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc) or type(exc).__name__


def _audit_request_id(idempotency_key: str | None) -> str | None:
    if idempotency_key is None or len(idempotency_key) <= 120:
        return idempotency_key
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SubscriptionCommandExecutionRejected",
    "execute_subscription_command",
]
