"""FUP runtime state management."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fup_state import FupActionStatus, FupState

logger = logging.getLogger(__name__)


class FupStateManager:
    """Manages per-subscription FUP enforcement state."""

    @staticmethod
    def get(db: Session, subscription_id: str) -> FupState | None:
        """Get the current FUP state for a subscription."""
        stmt = select(FupState).where(
            FupState.subscription_id == subscription_id
        )
        return db.scalars(stmt).first()

    @staticmethod
    def get_or_create(
        db: Session,
        subscription_id: str,
        offer_id: str,
    ) -> FupState:
        """Get existing state or create a clean one."""
        stmt = select(FupState).where(
            FupState.subscription_id == subscription_id
        )
        state = db.scalars(stmt).first()
        if state:
            return state
        state = FupState(
            subscription_id=uuid.UUID(subscription_id),
            offer_id=uuid.UUID(offer_id),
            action_status=FupActionStatus.none,
        )
        db.add(state)
        db.flush()
        return state

    @staticmethod
    def apply_action(
        db: Session,
        subscription_id: str,
        *,
        offer_id: str,
        rule_id: str | None = None,
        action_status: FupActionStatus,
        speed_reduction_percent: float | None = None,
        original_profile_id: str | None = None,
        throttle_profile_id: str | None = None,
        cap_resets_at: datetime | None = None,
        notes: str | None = None,
    ) -> FupState:
        """Record an enforcement action on a subscription."""
        state = FupStateManager.get_or_create(db, subscription_id, offer_id)
        state.active_rule_id = uuid.UUID(rule_id) if rule_id else None
        state.action_status = action_status
        state.speed_reduction_percent = speed_reduction_percent
        state.original_profile_id = (
            uuid.UUID(original_profile_id) if original_profile_id else None
        )
        state.throttle_profile_id = (
            uuid.UUID(throttle_profile_id) if throttle_profile_id else None
        )
        state.cap_resets_at = cap_resets_at
        state.last_evaluated_at = datetime.now(UTC)
        state.notes = notes
        db.flush()
        logger.info(
            "FUP state updated: subscription=%s action=%s rule=%s",
            subscription_id,
            action_status.value,
            rule_id,
        )
        return state

    @staticmethod
    def clear(db: Session, subscription_id: str) -> FupState | None:
        """Reset enforcement state (e.g. on period rollover)."""
        state = FupStateManager.get(db, subscription_id)
        if not state:
            return None
        state.active_rule_id = None
        state.action_status = FupActionStatus.none
        state.speed_reduction_percent = None
        state.throttle_profile_id = None
        state.last_evaluated_at = datetime.now(UTC)
        state.notes = "Period reset"
        db.flush()
        logger.info("FUP state cleared: subscription=%s", subscription_id)
        return state

    @staticmethod
    def list_throttled(db: Session) -> list[FupState]:
        """List all currently throttled/blocked subscriptions."""
        stmt = select(FupState).where(
            FupState.action_status.in_([
                FupActionStatus.throttled,
                FupActionStatus.blocked,
            ])
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def list_pending_reset(db: Session, before: datetime) -> list[FupState]:
        """List states with a cap_resets_at before the given time."""
        stmt = select(FupState).where(
            FupState.cap_resets_at.isnot(None),
            FupState.cap_resets_at <= before,
            FupState.action_status != FupActionStatus.none,
        )
        return list(db.scalars(stmt).all())


fup_state = FupStateManager()
