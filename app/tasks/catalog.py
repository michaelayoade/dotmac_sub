"""Celery tasks for catalog/subscription operations."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.celery_app import celery_app
from app.services.billing_settings import billing_enabled
from app.services.catalog import subscriptions as subscriptions_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

OPEN_INFRASTRUCTURE_TICKET_STATUSES = {
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "lastmile_rerun",
    "site_under_construction",
    "on_hold",
    "pending_confirmation",
}

INFRASTRUCTURE_DOWN_TICKET_MARKERS = (
    "infrastructure down",
    "service down",
    "internet down",
    "no internet",
    "outage",
    "link down",
    "fiber cut",
    "link disconnection",
    "customer link disconnection",
    "multiple customer link disconnection",
    "core link disconnection",
    "multiple core link disconnection",
    "cabinet disconnection",
    "multiple cabinet disconnection",
    "multiple cabinet link disconnection",
    "access point outage",
    "bts outage",
)


@celery_app.task(name="app.tasks.catalog.expire_subscriptions")
def expire_subscriptions() -> dict:
    """Expire subscriptions that have passed their end_at date."""
    logger.info("Starting expire_subscriptions")
    with db_session_adapter.session() as session:
        if not billing_enabled(session):
            logger.info(
                "expire_subscriptions skipped: local billing disabled (billing_enabled)"
            )
            return {"skipped": "billing_disabled"}
        result = subscriptions_service.expire_subscriptions(session)
        logger.info("Completed expire_subscriptions: %s", result)
        return result


@celery_app.task(name="app.tasks.catalog.apply_due_subscription_changes")
def apply_due_subscription_changes() -> dict:
    """Apply admin-scheduled next-cycle plan changes whose date has arrived.

    Swaps the offer for every ``approved`` (scheduled) SubscriptionChangeRequest
    with ``effective_date <= today``. Gated by ``billing_enabled`` because
    applying a plan change touches the recurring price and billing mode.
    """
    logger.info("Starting apply_due_subscription_changes")
    from app.services.subscription_changes import subscription_change_requests

    with db_session_adapter.session() as session:
        if not billing_enabled(session):
            logger.info(
                "apply_due_subscription_changes skipped: local billing disabled "
                "(billing_enabled)"
            )
            return {"skipped": "billing_disabled"}
        result = subscription_change_requests.apply_due_changes(session)
        logger.info("Completed apply_due_subscription_changes: %s", result)
        return result


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

    with db_session_adapter.session() as session:
        # Resolve configurable reminder days from settings
        if days_before is None:
            from app.models.domain_settings import SettingDomain
            from app.services.settings_spec import resolve_value

            days_before = int(
                resolve_value(session, SettingDomain.billing, "expiry_reminder_days")
                or 7
            )

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
        suppressed_subscriber_ids = _subscribers_with_open_infrastructure_down_tickets(
            session,
            {sub.subscriber_id for sub in expiring},
        )

        reminded = 0
        suppressed = 0
        for sub in expiring:
            try:
                if sub.subscriber_id in suppressed_subscriber_ids:
                    suppressed += 1
                    logger.info(
                        "Suppressed expiry reminder for subscription %s: "
                        "open infrastructure-down ticket exists",
                        sub.id,
                    )
                    continue
                end_at = _as_utc(sub.end_at)
                days_left = max(0, (end_at - now).days) if end_at else 0
                emit_event(
                    session,
                    EventType.subscription_expiring,
                    {
                        "days_remaining": str(days_left),
                        "end_date": end_at.strftime("%b %d, %Y") if end_at else "",
                        "plan_name": sub.offer.name if sub.offer else "your plan",
                    },
                    subscription_id=sub.id,
                    account_id=sub.subscriber_id,
                )
                reminded += 1
            except Exception as exc:
                logger.warning("Failed to send expiry reminder for %s: %s", sub.id, exc)

        session.commit()
        logger.info(
            "Sent %d expiry reminders; suppressed %d for infrastructure-down tickets",
            reminded,
            suppressed,
        )
    return {
        "reminded": reminded,
        "suppressed_infrastructure_down": suppressed,
        "total_expiring": len(expiring),
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _has_open_infrastructure_down_ticket(session, subscriber_id: object) -> bool:
    return subscriber_id in _subscribers_with_open_infrastructure_down_tickets(
        session,
        {subscriber_id},
    )


def _subscribers_with_open_infrastructure_down_tickets(
    session,
    subscriber_ids: set[object],
) -> set[object]:
    from sqlalchemy import or_

    from app.models.support import Ticket

    subscriber_ids = {
        subscriber_id for subscriber_id in subscriber_ids if subscriber_id
    }
    if not subscriber_ids:
        return set()

    tickets = (
        session.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(OPEN_INFRASTRUCTURE_TICKET_STATUSES))
        .filter(
            or_(
                Ticket.subscriber_id.in_(subscriber_ids),
                Ticket.customer_account_id.in_(subscriber_ids),
                Ticket.customer_person_id.in_(subscriber_ids),
            )
        )
        .all()
    )
    suppressed: set[object] = set()
    for ticket in tickets:
        if not _is_infrastructure_down_ticket(ticket):
            continue
        for field in ("subscriber_id", "customer_account_id", "customer_person_id"):
            ticket_subscriber_id = getattr(ticket, field, None)
            if ticket_subscriber_id in subscriber_ids:
                suppressed.add(ticket_subscriber_id)
    return suppressed


def _is_infrastructure_down_ticket(ticket: Any) -> bool:
    parts: list[str] = [
        str(getattr(ticket, "ticket_type", "") or ""),
        str(getattr(ticket, "title", "") or ""),
        str(getattr(ticket, "description", "") or ""),
    ]
    tags = getattr(ticket, "tags", None)
    if isinstance(tags, list):
        parts.extend(str(tag or "") for tag in tags)
    metadata = getattr(ticket, "metadata_", None)
    if isinstance(metadata, dict):
        for key in ("ticket_type", "category", "issue", "reason", "source"):
            parts.append(str(metadata.get(key) or ""))
    text = " ".join(parts).strip().lower()
    text = " ".join(text.split())
    return any(marker in text for marker in INFRASTRUCTURE_DOWN_TICKET_MARKERS)
