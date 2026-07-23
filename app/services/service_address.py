"""Canonical resolver for a subscriber's service ``Address``.

The single reader path for subscriber service location
(``docs/designs/SUBSCRIBER_SERVICE_LOCATION_SOT.md``). Consumers call this
instead of re-deriving the address or reading the legacy inline
``subscribers.address_*`` columns; it collapses the several near-duplicate
resolvers that grew up across the codebase. The customer/subscriber domain owns
the service ``Address``; this is how everyone else reads it.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Address, AddressType


def service_address(db: Session, subscriber_id: str | uuid.UUID) -> Address | None:
    """Resolve a subscriber's canonical service ``Address``, or ``None``.

    Resolution order (most authoritative first):

    1. the latest active subscription's ``service_address``;
    2. the primary service-type ``Address``;
    3. the primary ``Address`` (oldest);
    4. any ``Address`` (oldest).
    """
    active_subscription = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscription.service_address_id.isnot(None))
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if active_subscription and active_subscription.service_address:
        return active_subscription.service_address

    primary_service = (
        db.query(Address)
        .filter(Address.subscriber_id == subscriber_id)
        .filter(Address.address_type == AddressType.service)
        .filter(Address.is_primary.is_(True))
        .first()
    )
    if primary_service:
        return primary_service

    primary = (
        db.query(Address)
        .filter(Address.subscriber_id == subscriber_id)
        .filter(Address.is_primary.is_(True))
        .order_by(Address.created_at.asc())
        .first()
    )
    if primary:
        return primary

    return (
        db.query(Address)
        .filter(Address.subscriber_id == subscriber_id)
        .order_by(Address.created_at.asc())
        .first()
    )


def pick_service_address(addresses: list[Address] | None) -> Address | None:
    """Pick the service ``Address`` from an already-loaded list, or ``None``.

    The no-DB counterpart to :func:`service_address`, for formatters that
    already hold ``subscriber.addresses``. Prefers primary service-type, then
    primary, then any (stable on the list's existing order for ties).
    """
    items = list(addresses or [])
    if not items:
        return None
    items.sort(
        key=lambda a: (
            0 if getattr(a, "is_primary", False) else 1,
            0 if getattr(a, "address_type", None) == AddressType.service else 1,
        )
    )
    return items[0]
