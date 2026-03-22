"""Celery tasks for catalog/subscription operations."""

import logging
from datetime import UTC, datetime, timedelta

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services.catalog import subscriptions as subscriptions_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.catalog.expire_subscriptions")
def expire_subscriptions() -> dict:
    """Expire subscriptions that have passed their end_at date."""
    logger.info("Starting expire_subscriptions")
    session = SessionLocal()
    try:
        result = subscriptions_service.Subscriptions.expire_subscriptions(session)
        logger.info("Completed expire_subscriptions: %s", result)
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.catalog.send_expiry_reminders")
def send_expiry_reminders(days_before: int | None = None) -> dict:
    """Send renewal reminders for subscriptions expiring within N days.

    Emits subscription_expiring event for each matching subscription,
    which triggers the notification handler to queue emails/SMS.
    """
    from sqlalchemy import select

    from app.models.catalog import Subscription, SubscriptionStatus
    from app.services.events import emit_event
    from app.services.events.types import EventType

    session = SessionLocal()
    try:
        # Resolve configurable reminder days from settings
        if days_before is None:
            from app.models.domain_settings import SettingDomain
            from app.services.settings_spec import resolve_value

            days_before = int(resolve_value(session, SettingDomain.billing, "expiry_reminder_days") or 7)

        logger.info("Starting send_expiry_reminders (days_before=%d)", days_before)
        now = datetime.now(UTC)
        cutoff = now + timedelta(days=days_before)

        # Find active subscriptions expiring within the window
        from sqlalchemy.orm import joinedload

        stmt = (
            select(Subscription)
            .options(joinedload(Subscription.offer))
            .where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.end_at.isnot(None),
                Subscription.end_at <= cutoff,
                Subscription.end_at > now,
            )
        )
        expiring = session.scalars(stmt).unique().all()

        reminded = 0
        for sub in expiring:
            try:
                days_left = max(0, (sub.end_at - now).days) if sub.end_at else 0
                emit_event(
                    session,
                    EventType.subscription_expiring,
                    {
                        "days_remaining": str(days_left),
                        "end_date": sub.end_at.strftime("%b %d, %Y") if sub.end_at else "",
                        "plan_name": sub.offer.name if sub.offer else "your plan",
                    },
                    subscription_id=sub.id,
                    account_id=sub.subscriber_id,
                )
                reminded += 1
            except Exception as exc:
                logger.warning("Failed to send expiry reminder for %s: %s", sub.id, exc)

        session.commit()
        logger.info("Sent %d expiry reminders", reminded)
        return {"reminded": reminded, "total_expiring": len(expiring)}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
