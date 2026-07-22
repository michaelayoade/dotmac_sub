"""Celery tasks for vacation hold management."""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.services.db_session_adapter import db_session_adapter
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    resolve_subscription_lifecycle,
)
from app.services.subscription_lifecycle_commands import execute_subscription_command

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.vacation_holds.resume_expired_holds")
def resume_expired_holds() -> dict:
    """Resume subscriptions with expired vacation holds.

    Finds all active enforcement locks with reason=customer_hold
    that have resume_at in the past, and restores those subscriptions.

    Should be scheduled to run periodically (e.g., every hour or daily).
    """
    logger.info("Starting resume_expired_holds")
    session = SessionLocal()
    try:
        now = datetime.now(UTC)

        # Find expired vacation holds
        stmt = select(EnforcementLock).where(
            EnforcementLock.is_active.is_(True),
            EnforcementLock.reason == EnforcementReason.customer_hold,
            EnforcementLock.resume_at.isnot(None),
            EnforcementLock.resume_at <= now,
        )
        expired_holds = list(session.scalars(stmt).all())

        resumed = 0
        failed = 0
        for lock in expired_holds:
            try:
                snapshot = resolve_subscription_lifecycle(
                    session, str(lock.subscription_id)
                )
                outcome = execute_subscription_command(
                    session,
                    SubscriptionLifecycleCommand(
                        subscription_id=str(lock.subscription_id),
                        kind=SubscriptionCommandKind.vacation_resume,
                        source="customer:vacation_hold:auto_resume",
                        effective_timing=SubscriptionEffectiveTiming.immediate,
                        reason="Automatic resume after vacation hold period expired",
                        expected_head=snapshot.head,
                        idempotency_key=f"vacation-hold-auto-resume:{lock.id}",
                    ),
                )
                if outcome.status.value not in {"applied", "skipped"}:
                    failed += 1
                    logger.warning(
                        "Vacation auto-resume rejected for subscription %s: %s",
                        lock.subscription_id,
                        outcome.message,
                    )
                    continue
                subscription = session.get(Subscription, lock.subscription_id)
                restored = bool(
                    subscription is not None
                    and subscription.status == SubscriptionStatus.active
                )
                if restored:
                    resumed += 1
                    logger.info(
                        "Auto-resumed subscription %s (lock=%s, resume_at=%s)",
                        lock.subscription_id,
                        lock.id,
                        lock.resume_at,
                    )
                else:
                    # Lock was resolved but other locks may still exist
                    logger.info(
                        "Resolved vacation hold for subscription %s but not fully restored (other locks active)",
                        lock.subscription_id,
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Failed to auto-resume subscription %s (lock=%s): %s",
                    lock.subscription_id,
                    lock.id,
                    exc,
                )

        logger.info(
            "Completed resume_expired_holds: %d resumed, %d failed, %d total",
            resumed,
            failed,
            len(expired_holds),
        )
        return {
            "total": len(expired_holds),
            "resumed": resumed,
            "failed": failed,
        }
    finally:
        session.close()
