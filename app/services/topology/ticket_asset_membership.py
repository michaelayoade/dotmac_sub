"""Subscription observations used by the ticket asset audience resolver."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)


class WirelessSubscriptionMatch(Enum):
    conflicting = "conflicting"
    inactive = "inactive"
    active = "active"


def wireless_subscription_match(
    db: Session, *, subscription_id: UUID, subscriber_id: UUID | None
) -> WirelessSubscriptionMatch:
    """Read the exact subscription and access type behind one customer radio."""
    subscription = db.get(Subscription, subscription_id)
    if (
        subscription is None
        or subscription.subscriber_id != subscriber_id
        or subscription.offer is None
        or subscription.offer.access_type is not AccessType.fixed_wireless
    ):
        return WirelessSubscriptionMatch.conflicting
    if subscription.status == SubscriptionStatus.active:
        return WirelessSubscriptionMatch.active
    return WirelessSubscriptionMatch.inactive


def active_fiber_subscription_ids(db: Session) -> tuple[UUID, ...]:
    """Read the complete active fiber cohort for route verification."""
    return tuple(
        db.scalars(
            select(Subscription.id)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(
                Subscription.status == SubscriptionStatus.active,
                CatalogOffer.access_type == AccessType.fiber,
            )
            .order_by(Subscription.id)
        ).all()
    )
