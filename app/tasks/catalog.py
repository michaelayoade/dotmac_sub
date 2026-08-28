"""Celery tasks for catalog/subscription operations."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from app.celery_app import celery_app
from app.services.catalog import subscriptions as subscriptions_service

# Outage/ticket suppression predicates live in the shared customer-service-state
# service (single source of truth for outage-aware comms); re-exported here so
# existing callers and tests keep their import/monkeypatch paths.
from app.services.customer_service_state import (
    INFRASTRUCTURE_DOWN_TICKET_MARKERS,  # noqa: F401 - re-export
    OPEN_INFRASTRUCTURE_TICKET_STATUSES,  # noqa: F401 - re-export
)
from app.services.customer_service_state import (
    is_infrastructure_down_ticket as _is_infrastructure_down_ticket,  # noqa: F401
)
from app.services.customer_service_state import (
    subscribers_with_open_infrastructure_down_tickets as _subscribers_with_open_infrastructure_down_tickets,
)
from app.services.customer_service_state import (
    subscription_ids_under_active_outage as _subscription_ids_under_active_outage,
)
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models.catalog import Subscription


@dataclass(frozen=True, slots=True)
class ExpiryReminderCandidate:
    """A subscription and the boundary this reminder is about."""

    subscription: "Subscription"
    boundary: datetime
    boundary_key: str
    source: str


@celery_app.task(name="app.tasks.catalog.expire_subscriptions")
def expire_subscriptions() -> dict:
    """Expire subscriptions that have passed their end_at date."""
    logger.info("Starting expire_subscriptions")
    with db_session_adapter.session() as session:
        result = subscriptions_service.expire_subscriptions(session)
        logger.info("Completed expire_subscriptions: %s", result)
        return result


@celery_app.task(name="app.tasks.catalog.apply_due_subscription_changes")
def apply_due_subscription_changes() -> dict:
    """Apply admin-scheduled next-cycle plan changes whose date has arrived.

    Swaps the offer for every ``approved`` (scheduled) SubscriptionChangeRequest
    with ``effective_date <= today``.
    """
    logger.info("Starting apply_due_subscription_changes")
    from app.services.subscription_changes import subscription_change_requests

    with db_session_adapter.session() as session:
        result = subscription_change_requests.apply_due_changes(session)
        logger.info("Completed apply_due_subscription_changes: %s", result)
        return result


@celery_app.task(name="app.tasks.catalog.apply_due_subscription_status_commands")
def apply_due_subscription_status_commands() -> dict:
    """Apply due deferred status commands through the canonical executor."""
    from app.services.subscription_lifecycle_schedules import (
        apply_due_subscription_status_commands as apply_due,
    )

    logger.info("Starting apply_due_subscription_status_commands")
    with db_session_adapter.session() as session:
        result = apply_due(session)
        logger.info("Completed apply_due_subscription_status_commands: %s", result)
        return result


@celery_app.task(name="app.tasks.catalog.send_expiry_reminders")
def send_expiry_reminders(days_before: int | None = None) -> dict:
    """Send renewal reminders for subscriptions expiring within N days.

    Emits subscription_expiring event for each matching subscription,
    which triggers the notification handler to queue emails/SMS.
    """
    from sqlalchemy import or_, select

    from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
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

        # For prepaid services, the renewal/access boundary is next_billing_at.
        # end_at remains a contract-end fallback for rows without a renewal anchor.
        from sqlalchemy.orm import joinedload

        stmt = (
            select(Subscription)
            .options(joinedload(Subscription.offer))
            .where(
                Subscription.status == SubscriptionStatus.active,
                or_(
                    (
                        (Subscription.billing_mode == BillingMode.prepaid)
                        & Subscription.next_billing_at.isnot(None)
                        & (Subscription.next_billing_at <= cutoff)
                        & (Subscription.next_billing_at > now)
                    ),
                    (
                        Subscription.end_at.isnot(None)
                        & (Subscription.end_at <= cutoff)
                        & (Subscription.end_at > now)
                    ),
                ),
            )
        )
        subscriptions = session.scalars(stmt).unique().all()
        expiring = _expiry_reminder_candidates(subscriptions, now=now, cutoff=cutoff)
        reminded_periods = _subscription_expiring_reminder_periods(
            session,
            {candidate.subscription.id for candidate in expiring},
        )
        suppressed_subscriber_ids = _subscribers_with_open_infrastructure_down_tickets(
            session,
            {candidate.subscription.subscriber_id for candidate in expiring},
        )
        outage_subscription_ids = _subscription_ids_under_active_outage(
            session, [candidate.subscription for candidate in expiring]
        )

        reminded = 0
        suppressed = 0
        suppressed_outage = 0
        duplicate_periods = 0
        for candidate in expiring:
            sub = candidate.subscription
            boundary = candidate.boundary
            boundary_key = candidate.boundary_key
            try:
                if sub.subscriber_id in suppressed_subscriber_ids:
                    suppressed += 1
                    logger.info(
                        "Suppressed expiry reminder for subscription %s: "
                        "open infrastructure-down ticket exists",
                        sub.id,
                    )
                    continue
                if sub.id in outage_subscription_ids:
                    suppressed_outage += 1
                    logger.info(
                        "Suppressed expiry reminder for subscription %s: "
                        "active outage incident covers its path",
                        sub.id,
                    )
                    continue
                if boundary_key in reminded_periods.get(sub.id, set()):
                    duplicate_periods += 1
                    logger.info(
                        "Skipped duplicate expiry reminder for subscription %s "
                        "boundary %s",
                        sub.id,
                        boundary_key,
                    )
                    continue
                days_left = max(0, (boundary - now).days)
                emit_event(
                    session,
                    EventType.subscription_expiring,
                    {
                        "days_remaining": str(days_left),
                        "end_date": boundary.strftime("%b %d, %Y"),
                        "plan_name": sub.offer.name if sub.offer else "your plan",
                        "reminder_boundary": boundary_key,
                        "reminder_boundary_source": candidate.source,
                    },
                    subscription_id=sub.id,
                    account_id=sub.subscriber_id,
                )
                reminded_periods.setdefault(sub.id, set()).add(boundary_key)
                reminded += 1
            except Exception as exc:
                logger.warning("Failed to send expiry reminder for %s: %s", sub.id, exc)

        session.commit()
        logger.info(
            "Sent %d expiry reminders; suppressed %d for infrastructure-down "
            "tickets, %d for active outage incidents, skipped %d duplicate periods",
            reminded,
            suppressed,
            suppressed_outage,
            duplicate_periods,
        )
    return {
        "reminded": reminded,
        "suppressed_infrastructure_down": suppressed,
        "suppressed_active_outage": suppressed_outage,
        "duplicate_periods": duplicate_periods,
        "total_expiring": len(expiring),
    }


def _expiry_reminder_candidates(
    subscriptions: list["Subscription"],
    *,
    now: datetime,
    cutoff: datetime,
) -> list[ExpiryReminderCandidate]:
    from app.models.catalog import BillingMode

    candidates: list[ExpiryReminderCandidate] = []
    for subscription in subscriptions:
        source = "end_at"
        boundary = _as_utc(subscription.end_at)
        next_billing_at = _as_utc(subscription.next_billing_at)
        if subscription.billing_mode == BillingMode.prepaid and next_billing_at:
            source = "next_billing_at"
            boundary = next_billing_at
        if boundary is None or boundary <= now or boundary > cutoff:
            continue
        boundary_key = _reminder_boundary_key(boundary)
        candidates.append(
            ExpiryReminderCandidate(
                subscription=subscription,
                boundary=boundary,
                boundary_key=boundary_key,
                source=source,
            )
        )
    return candidates


def _subscription_expiring_reminder_periods(
    session,
    subscription_ids: set[UUID],
) -> dict[UUID, set[str]]:
    if not subscription_ids:
        return {}

    from sqlalchemy import select

    from app.models.event_store import EventStore
    from app.services.events.types import EventType

    rows = session.execute(
        select(EventStore.subscription_id, EventStore.payload).where(
            EventStore.event_type == EventType.subscription_expiring.value,
            EventStore.subscription_id.in_(subscription_ids),
        )
    ).all()
    periods: dict[UUID, set[str]] = {}
    for subscription_id, payload in rows:
        if subscription_id is None or not isinstance(payload, dict):
            continue
        boundary = payload.get("reminder_boundary")
        if not isinstance(boundary, str) or not boundary:
            continue
        normalized_boundary = _reminder_boundary_key_from_string(boundary)
        if normalized_boundary is None:
            continue
        periods.setdefault(subscription_id, set()).add(normalized_boundary)
    return periods


def _reminder_boundary_key(value: datetime) -> str:
    utc_value = _as_utc(value)
    if utc_value is None:
        raise ValueError("reminder boundary is required")
    return utc_value.isoformat()


def _reminder_boundary_key_from_string(value: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _reminder_boundary_key(parsed)


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
