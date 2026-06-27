"""IPv6 PD pool-selection policy + provision-on-activation (flag-gated)."""

from __future__ import annotations

import uuid

from app.models.catalog import RadiusProfile, Subscription, SubscriptionStatus
from app.models.network import IpPool, IPVersion
from app.services import ipv6_pd


def _v6pool(db, name, cidr="2001:db8::/48", length=64):
    pool = IpPool(
        id=uuid.uuid4(),
        name=name,
        ip_version=IPVersion.ipv6,
        cidr=cidr,
        is_active=True,
        delegation_prefix_length=length,
    )
    db.add(pool)
    db.flush()
    return pool


def _subscription(db, subscriber, catalog_offer, profile=None):
    if profile is not None:
        db.add(profile)
        db.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        radius_profile_id=profile.id if profile else None,
    )
    db.add(sub)
    db.flush()
    return sub


def test_resolve_pd_pool_by_profile_name(db_session, subscriber, catalog_offer):
    _v6pool(db_session, "pool-a", cidr="2001:db8:a::/48")
    p2 = _v6pool(db_session, "pool-b", cidr="2001:db8:b::/48")
    prof = RadiusProfile(name="P", ipv6_pool_name="pool-b")
    sub = _subscription(db_session, subscriber, catalog_offer, profile=prof)
    assert ipv6_pd.resolve_pd_pool(db_session, sub).id == p2.id


def test_resolve_pd_pool_single_fallback(db_session, subscriber, catalog_offer):
    p1 = _v6pool(db_session, "only")
    sub = _subscription(db_session, subscriber, catalog_offer)
    assert ipv6_pd.resolve_pd_pool(db_session, sub).id == p1.id


def test_resolve_pd_pool_multi_without_match_is_none(
    db_session, subscriber, catalog_offer
):
    _v6pool(db_session, "a", cidr="2001:db8:a::/48")
    _v6pool(db_session, "b", cidr="2001:db8:b::/48")
    sub = _subscription(db_session, subscriber, catalog_offer)
    assert ipv6_pd.resolve_pd_pool(db_session, sub) is None


def test_resolve_pd_pool_ignores_pools_without_delegation_size(
    db_session, subscriber, catalog_offer
):
    # An IPv6 pool with no delegation size is not a PD pool.
    plain = IpPool(
        id=uuid.uuid4(),
        name="plain",
        ip_version=IPVersion.ipv6,
        cidr="2001:db8:c::/48",
        is_active=True,
        delegation_prefix_length=None,
    )
    db_session.add(plain)
    db_session.flush()
    sub = _subscription(db_session, subscriber, catalog_offer)
    assert ipv6_pd.resolve_pd_pool(db_session, sub) is None


def test_provision_pd_is_flag_gated_and_idempotent(
    db_session, subscriber, catalog_offer, monkeypatch
):
    _v6pool(db_session, "only")
    sub = _subscription(db_session, subscriber, catalog_offer)

    monkeypatch.delenv("IPV6_PD_ENABLED", raising=False)
    assert ipv6_pd.provision_pd_for_subscription(db_session, sub) is None  # off

    monkeypatch.setenv("IPV6_PD_ENABLED", "true")
    pd = ipv6_pd.provision_pd_for_subscription(db_session, sub)
    assert pd is not None
    assert pd.subscriber_id == subscriber.id
    assert pd.subscription_id == sub.id
    # Idempotent: same subscriber keeps its prefix.
    pd2 = ipv6_pd.provision_pd_for_subscription(db_session, sub)
    assert pd2.id == pd.id


def test_manual_assign_action(db_session, subscriber):
    from app.services.web_network_ip import assign_delegated_prefix_action

    pool = _v6pool(db_session, "manual")
    db_session.commit()
    err = assign_delegated_prefix_action(
        db_session, pool_id=str(pool.id), subscriber_id=str(subscriber.id)
    )
    assert err is None
    assert (
        ipv6_pd.active_delegated_prefix_for_subscriber(db_session, subscriber.id)
        is not None
    )


def test_manual_assign_rejects_unknown_subscriber(db_session):
    from app.services.web_network_ip import assign_delegated_prefix_action

    pool = _v6pool(db_session, "manual2")
    db_session.commit()
    err = assign_delegated_prefix_action(
        db_session, pool_id=str(pool.id), subscriber_id=str(uuid.uuid4())
    )
    assert err is not None
