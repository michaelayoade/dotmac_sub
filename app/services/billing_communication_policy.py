"""Shared billing/dunning communication suppression policy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.customer_service_state import (
    active_outage_subscription_ids,
    subscribers_with_open_infrastructure_down_tickets,
)


@dataclass(frozen=True)
class BillingCommunicationDecision:
    subscription_id: object
    subscriber_id: object | None
    suppress_expiry_notice: bool
    suppress_suspension_notice: bool
    suppress_dunning_notice: bool
    reason: str | None


def billing_communication_decisions(
    db: Session,
    subscriptions,
) -> dict[object, BillingCommunicationDecision]:
    """Return billing-comms decisions for a batch of subscriptions."""
    rows = list(subscriptions)
    subscription_ids = {sub.id for sub in rows if getattr(sub, "id", None)}
    subscriber_ids = {
        sub.subscriber_id for sub in rows if getattr(sub, "subscriber_id", None)
    }
    outage_ids = active_outage_subscription_ids(db) & subscription_ids
    ticket_subscribers = subscribers_with_open_infrastructure_down_tickets(
        db, subscriber_ids
    )

    decisions: dict[object, BillingCommunicationDecision] = {}
    for subscription in rows:
        subscription_id = getattr(subscription, "id", None)
        if subscription_id is None:
            continue
        subscriber_id = getattr(subscription, "subscriber_id", None)
        if subscription_id in outage_ids:
            reason = "active_infrastructure_outage"
        elif subscriber_id in ticket_subscribers:
            reason = "open_infrastructure_down_ticket"
        else:
            reason = None
        suppress = reason is not None
        decisions[subscription_id] = BillingCommunicationDecision(
            subscription_id=subscription_id,
            subscriber_id=subscriber_id,
            suppress_expiry_notice=suppress,
            suppress_suspension_notice=suppress,
            suppress_dunning_notice=suppress,
            reason=reason,
        )
    return decisions
