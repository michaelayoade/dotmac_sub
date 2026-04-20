"""Celery tasks for vacation hold management."""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.services.account_lifecycle import restore_subscription
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.vacation_holds.resume_expired_holds")
def resume_expired_holds() -> dict:
    """Resume subscriptions with expired vacation holds.

    Finds all active enforcement locks with reason=customer_hold
    that have resume_at in the past, and restores those subscriptions.

    Should be scheduled to run periodically (e.g., every hour or daily).
    """
    logger.info("Starting resume_expired_holds")
    with db_session_adapter.session() as session:
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
                # Use "customer" trigger since auto-resume honors customer's scheduled time
                restored = restore_subscription(
                    session,
                    str(lock.subscription_id),
                    trigger="customer",
                    resolved_by="vacation_hold:auto_resume",
                    reason=EnforcementReason.customer_hold,
                    notes="Automatic resume after vacation hold period expired",
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

        session.commit()
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
