"""Canonical participant writer for per-subscription FUP runtime state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.fup_state import FupActionStatus, FupState
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event

logger = logging.getLogger(__name__)


class FupRuntimeStateError(DomainError):
    """Stable failures at the FUP runtime-state owner boundary."""


def _error(suffix: str, message: str) -> FupRuntimeStateError:
    return FupRuntimeStateError(
        code=f"access.fup_runtime_state.{suffix}",
        message=message,
    )


def _uuid(value: UUID | str, *, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise _error(
            f"invalid_{field}", f"{field.replace('_', ' ').title()} is invalid."
        ) from exc


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _error(
            f"invalid_{field}",
            f"{field.replace('_', ' ').title()} must be timezone-aware.",
        )
    return value


@dataclass(frozen=True, slots=True)
class ApplyFupRuntimeState:
    subscription_id: UUID
    offer_id: UUID
    action_status: FupActionStatus
    evaluated_at: datetime
    rule_id: UUID | None = None
    speed_reduction_percent: float | None = None
    original_profile_id: UUID | None = None
    throttle_profile_id: UUID | None = None
    cap_resets_at: datetime | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _aware(self.evaluated_at, field="evaluated_at")
        if self.cap_resets_at is not None:
            _aware(self.cap_resets_at, field="cap_resets_at")


@dataclass(frozen=True, slots=True)
class ClearFupRuntimeState:
    subscription_id: UUID
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _aware(self.evaluated_at, field="evaluated_at")


def _locked_subscription(db: Session, subscription_id: UUID) -> Subscription:
    subscription = db.scalar(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    )
    if subscription is None:
        raise _error("subscription_not_found", "Subscription was not found.")
    return subscription


def _locked_state(db: Session, subscription_id: UUID) -> FupState | None:
    return db.scalar(
        select(FupState)
        .where(FupState.subscription_id == subscription_id)
        .with_for_update()
    )


def _get_or_create(
    db: Session,
    *,
    subscription: Subscription,
    offer_id: UUID,
) -> FupState:
    if subscription.offer_id != offer_id:
        raise _error(
            "offer_mismatch",
            "FUP runtime state offer does not match the subscription offer.",
        )
    state = _locked_state(db, subscription.id)
    if state is not None:
        if state.offer_id != offer_id:
            raise _error(
                "state_offer_mismatch",
                "Existing FUP runtime state has conflicting offer evidence.",
            )
        return state
    state = FupState(
        subscription_id=subscription.id,
        offer_id=offer_id,
        action_status=FupActionStatus.none,
    )
    db.add(state)
    db.flush()
    return state


def _emit_transition(
    db: Session,
    state: FupState,
    *,
    transition: str,
) -> None:
    emit_event(
        db,
        EventType.fup_runtime_state_changed,
        {
            "schema_version": 1,
            "subscription_id": str(state.subscription_id),
            "offer_id": str(state.offer_id),
            "transition": transition,
            "action_status": state.action_status.value,
        },
        subscription_id=state.subscription_id,
    )


class FupStateManager:
    """Read and nested-write API for canonical FUP runtime state."""

    @staticmethod
    def get(db: Session, subscription_id: UUID | str) -> FupState | None:
        resolved_id = _uuid(subscription_id, field="subscription_id")
        return db.scalar(
            select(FupState).where(FupState.subscription_id == resolved_id)
        )

    @staticmethod
    def apply_action(db: Session, command: ApplyFupRuntimeState) -> FupState:
        """Stage one locked runtime-state transition in the caller transaction."""
        subscription = _locked_subscription(db, command.subscription_id)
        state = _get_or_create(
            db,
            subscription=subscription,
            offer_id=command.offer_id,
        )
        proposed = (
            command.rule_id,
            command.action_status,
            command.speed_reduction_percent,
            command.original_profile_id,
            command.throttle_profile_id,
            command.cap_resets_at,
            command.evaluated_at,
            command.notes,
        )
        current = (
            state.active_rule_id,
            state.action_status,
            state.speed_reduction_percent,
            state.original_profile_id,
            state.throttle_profile_id,
            state.cap_resets_at,
            state.last_evaluated_at,
            state.notes,
        )
        if current == proposed:
            return state
        (
            state.active_rule_id,
            state.action_status,
            state.speed_reduction_percent,
            state.original_profile_id,
            state.throttle_profile_id,
            state.cap_resets_at,
            state.last_evaluated_at,
            state.notes,
        ) = proposed
        _emit_transition(db, state, transition="action_applied")
        db.flush()
        logger.info(
            "fup_runtime_state_updated",
            extra={
                "subscription_id": str(command.subscription_id),
                "action_status": command.action_status.value,
                "rule_id": str(command.rule_id) if command.rule_id else None,
            },
        )
        return state

    @staticmethod
    def clear(db: Session, command: ClearFupRuntimeState) -> FupState | None:
        """Stage an idempotent locked period-reset transition."""
        _locked_subscription(db, command.subscription_id)
        state = _locked_state(db, command.subscription_id)
        if state is None:
            return None
        if (
            state.active_rule_id is None
            and state.action_status is FupActionStatus.none
            and state.speed_reduction_percent is None
            and state.original_profile_id is None
            and state.throttle_profile_id is None
            and state.cap_resets_at is None
        ):
            return state
        state.active_rule_id = None
        state.action_status = FupActionStatus.none
        state.speed_reduction_percent = None
        state.original_profile_id = None
        state.throttle_profile_id = None
        state.cap_resets_at = None
        state.last_evaluated_at = command.evaluated_at
        state.notes = "Period reset"
        _emit_transition(db, state, transition="state_cleared")
        db.flush()
        logger.info(
            "fup_runtime_state_cleared",
            extra={"subscription_id": str(command.subscription_id)},
        )
        return state

    @staticmethod
    def list_throttled(db: Session) -> list[FupState]:
        """List all currently throttled or blocked subscriptions."""
        stmt = select(FupState).where(
            FupState.action_status.in_(
                [FupActionStatus.throttled, FupActionStatus.blocked]
            )
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def list_pending_reset(db: Session, before: datetime) -> list[FupState]:
        """List active states whose canonical cap reset is due."""
        _aware(before, field="before")
        stmt = select(FupState).where(
            FupState.cap_resets_at.isnot(None),
            FupState.cap_resets_at <= before,
            FupState.action_status != FupActionStatus.none,
        )
        return list(db.scalars(stmt).all())


fup_state = FupStateManager()

__all__ = [
    "ApplyFupRuntimeState",
    "ClearFupRuntimeState",
    "FupRuntimeStateError",
    "FupStateManager",
    "fup_state",
]
