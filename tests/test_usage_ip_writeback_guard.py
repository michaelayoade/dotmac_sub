"""Accounting framed-IP observations never overwrite the desired projection."""

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


def test_active_sub_ip_is_observation_only(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer, SubscriptionStatus.active)
    _write_subscription_ips_from_accounting(
        db_session, sub.id, ipv4="10.0.0.9", ipv6=None
    )
    assert sub.ipv4_address == "10.0.0.5"
    assert sub.last_seen_framed_ipv4 == "10.0.0.9"


def test_suspended_sub_ip_not_overwritten(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer, SubscriptionStatus.suspended)
    _write_subscription_ips_from_accounting(
        db_session, sub.id, ipv4="100.64.0.1", ipv6=None
    )
    db_session.flush()
    db_session.refresh(sub)
    assert sub.ipv4_address == "10.0.0.5"  # unchanged
    assert sub.last_seen_framed_ipv4 == "100.64.0.1"


def test_terminated_sub_ip_not_overwritten(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer, SubscriptionStatus.canceled)
    _write_subscription_ips_from_accounting(
        db_session, sub.id, ipv4="100.64.0.2", ipv6=None
    )
    db_session.flush()
    db_session.refresh(sub)
    assert sub.ipv4_address == "10.0.0.5"  # unchanged
    assert sub.last_seen_framed_ipv4 == "100.64.0.2"
