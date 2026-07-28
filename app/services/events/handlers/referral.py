"""Referral-qualification hook on subscriber activation.

The CRM qualified referrals from its customer-sync path (every sub→CRM
subscriber sync re-checked qualification). Natively the trigger is sub's own
lifecycle events: when a referred prospect's service goes active, the pending
referral qualifies and the referrer earns the configured reward.

``subscription.activated`` is the activation moment (``activate_subscription``
emits it before ``compute_account_status`` re-derives the account flag — the
service's active check looks at subscriptions too, so the ordering is safe).
The subscriber-status events cover admin-driven flips to ``active``.
The handler is a thin adapter: it submits a typed, idempotent qualification
command. The program owner locks and commits its own complete transaction.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.db_session_adapter import db_session_adapter
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.referrals import (
    REFERRAL_PROGRAM_SCOPE,
    QualifyReferralForSubscriberCommand,
    qualify_referral_for_subscriber,
)

logger = logging.getLogger(__name__)

# Events that can turn a referred prospect into an active subscriber.
REFERRAL_QUALIFY_EVENTS = {
    EventType.subscription_activated,
    EventType.subscriber_created,
    EventType.subscriber_updated,
    EventType.subscriber_reactivated,
}


class ReferralHandler:
    """Qualify pending referrals when the referred subscriber activates."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in REFERRAL_QUALIFY_EVENTS:
            return
        if not isinstance(db, Session):
            return

        subscriber_id = (
            event.account_id
            or event.subscriber_id
            or event.payload.get("account_id")
            or event.payload.get("subscriber_id")
        )
        if not subscriber_id:
            return

        try:
            resolved_subscriber_id = UUID(str(subscriber_id))
        except ValueError:
            return

        db_session_adapter.release_read_transaction(db)
        result = qualify_referral_for_subscriber(
            db,
            QualifyReferralForSubscriberCommand(
                context=CommandContext.system(
                    actor="subscriber_lifecycle_event",
                    scope=REFERRAL_PROGRAM_SCOPE,
                    reason="Subscriber lifecycle event requested referral qualification",
                    correlation_id=event.event_id,
                    causation_id=event.event_id,
                    idempotency_key=f"referral-qualification:{event.event_id}",
                ),
                subscriber_id=resolved_subscriber_id,
            ),
        )
        if result.referral_id is not None and result.outcome != "not_applicable":
            logger.info(
                "referral_qualification_handled event=%s subscriber=%s referral=%s status=%s",
                event.event_type.value,
                resolved_subscriber_id,
                result.referral_id,
                result.status,
            )
