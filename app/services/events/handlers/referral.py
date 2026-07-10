"""Referral-qualification hook on subscriber activation (Phase 3 §2.1).

The CRM qualified referrals from its customer-sync path (every sub→CRM
subscriber sync re-checked qualification). Natively the trigger is sub's own
lifecycle events: when a referred prospect's service goes active, the pending
referral qualifies and the referrer earns the configured reward.

``subscription.activated`` is the activation moment (``activate_subscription``
emits it before ``compute_account_status`` re-derives the account flag — the
service's active check looks at subscriptions too, so the ordering is safe).
The subscriber-status events cover admin-driven flips to ``active``.
``Referrals.qualify_for_subscriber`` is idempotent and flush-only, so handling
a broad event set is cheap and can never double-qualify or commit the
emitting service's open transaction.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services.events.types import Event, EventType

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

        subscriber = db.get(Subscriber, subscriber_id)
        if subscriber is None:
            return

        from app.services.referrals import referrals

        referral = referrals.qualify_for_subscriber(db, subscriber)
        if referral is not None:
            logger.info(
                "referral_qualification_handled event=%s subscriber=%s referral=%s status=%s",
                event.event_type.value,
                subscriber.id,
                referral.id,
                referral.status,
            )
