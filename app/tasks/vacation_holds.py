"""Celery tasks for vacation hold management."""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscription_pause import (
    SubscriptionPauseCause,
    SubscriptionPauseCauseStatus,
    SubscriptionPauseEpisode,
    SubscriptionPauseEpisodeStatus,
    SubscriptionPauseReason,
)
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

    Finds all active customer-vacation pause causes whose scheduled resume
    instant has passed and releases those causes through the lifecycle owner.

    Should be scheduled to run periodically (e.g., every hour or daily).
    """
    logger.info("Starting resume_expired_holds")
    session = SessionLocal()
    try:
        now = datetime.now(UTC)

        stmt = (
            select(SubscriptionPauseCause)
            .join(
                SubscriptionPauseEpisode,
                SubscriptionPauseEpisode.id == SubscriptionPauseCause.pause_episode_id,
            )
            .where(
                SubscriptionPauseCause.status
                == SubscriptionPauseCauseStatus.active.value,
                SubscriptionPauseCause.reason_code
                == SubscriptionPauseReason.customer_vacation_hold.value,
                SubscriptionPauseCause.scheduled_resume_at.isnot(None),
                SubscriptionPauseCause.scheduled_resume_at <= now,
                SubscriptionPauseEpisode.status
                == SubscriptionPauseEpisodeStatus.active.value,
            )
        )
        expired_holds = list(session.scalars(stmt).all())

        resumed = 0
        failed = 0
        for cause in expired_holds:
            episode = None
            try:
                episode = session.get(SubscriptionPauseEpisode, cause.pause_episode_id)
                if episode is None:
                    raise ValueError("Vacation-hold pause episode is missing")
                snapshot = resolve_subscription_lifecycle(
                    session, str(episode.subscription_id)
                )
                outcome = execute_subscription_command(
                    session,
                    SubscriptionLifecycleCommand(
                        subscription_id=str(episode.subscription_id),
                        kind=SubscriptionCommandKind.vacation_resume,
                        source="customer:vacation_hold:auto_resume",
                        effective_timing=SubscriptionEffectiveTiming.immediate,
                        reason="Automatic resume after vacation hold period expired",
                        expected_head=snapshot.head,
                        idempotency_key=f"vacation-hold-auto-resume:{cause.id}",
                    ),
                )
                if outcome.status.value not in {"applied", "skipped"}:
                    failed += 1
                    logger.warning(
                        "Vacation auto-resume rejected for subscription %s: %s",
                        episode.subscription_id,
                        outcome.message,
                    )
                    continue
                subscription = session.get(Subscription, episode.subscription_id)
                restored = bool(
                    subscription is not None
                    and subscription.status == SubscriptionStatus.active
                )
                if restored:
                    resumed += 1
                    logger.info(
                        "Auto-resumed subscription %s (cause=%s, resume_at=%s)",
                        episode.subscription_id,
                        cause.id,
                        cause.scheduled_resume_at,
                    )
                else:
                    # This cause was released but another pause cause or access
                    # restriction still prevents active service.
                    logger.info(
                        "Resolved vacation hold for subscription %s but not fully restored",
                        episode.subscription_id,
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Failed to auto-resume subscription %s (cause=%s): %s",
                    episode.subscription_id if episode is not None else "unknown",
                    cause.id,
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
