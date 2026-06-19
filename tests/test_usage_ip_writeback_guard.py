"""Accounting framed-IP write-back is gated to active subscriptions (task #15).

A stale/reject-pool address on a suspended/terminated subscriber's accounting
row must not overwrite the served-IP column (which the RADIUS sweep re-emits).
"""

from __future__ import annotations

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.usage import _write_subscription_ips_from_accounting


def _sub(db, subscriber, catalog_offer, status):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
        ipv4_address="10.0.0.5",
    )
    db.add(sub)
    db.flush()
    return sub


def test_active_sub_ip_is_mirrored(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer, SubscriptionStatus.active)
    _write_subscription_ips_from_accounting(
        db_session, sub.id, ipv4="10.0.0.9", ipv6=None
    )
    assert sub.ipv4_address == "10.0.0.9"


def test_suspended_sub_ip_not_overwritten(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer, SubscriptionStatus.suspended)
    _write_subscription_ips_from_accounting(
        db_session, sub.id, ipv4="100.64.0.1", ipv6=None
    )
    db_session.refresh(sub)
    assert sub.ipv4_address == "10.0.0.5"  # unchanged


def test_terminated_sub_ip_not_overwritten(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer, SubscriptionStatus.canceled)
    _write_subscription_ips_from_accounting(
        db_session, sub.id, ipv4="100.64.0.2", ipv6=None
    )
    db_session.refresh(sub)
    assert sub.ipv4_address == "10.0.0.5"  # unchanged
