"""IPv6 prefix-delegation allocator."""

from __future__ import annotations

import uuid

from app.models.network import IpPool, IPVersion, Ipv6PrefixState
from app.models.subscriber import Subscriber
from app.services import ipv6_pd


def _pool(db, cidr="2001:db8::/48", length=64):
    pool = IpPool(
        id=uuid.uuid4(),
        name=f"v6-{uuid.uuid4().hex[:6]}",
        ip_version=IPVersion.ipv6,
        cidr=cidr,
        is_active=True,
        delegation_prefix_length=length,
    )
    db.add(pool)
    db.flush()
    return pool


def _sub(db):
    s = Subscriber(first_name="A", last_name="L", email=f"{uuid.uuid4().hex[:8]}@e.com")
    db.add(s)
    db.flush()
    return s


def test_allocate_materializes_first_prefix(db_session):
    pool = _pool(db_session)
    sub = _sub(db_session)
    pd = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
    assert pd is not None
    assert pd.prefix == "2001:db8::"
    assert pd.prefix_length == 64
    assert pd.state == Ipv6PrefixState.assigned
    assert pd.subscriber_id == sub.id


def test_idempotent_for_same_subscriber(db_session):
    pool = _pool(db_session)
    sub = _sub(db_session)
    pd1 = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
    pd2 = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
    assert pd1.id == pd2.id


def test_two_subscribers_get_distinct_prefixes(db_session):
    pool = _pool(db_session)
    a, b = _sub(db_session), _sub(db_session)
    pa = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=a.id)
    pb = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=b.id)
    assert pa.prefix != pb.prefix
    assert pb.prefix == "2001:db8:0:1::"  # next aligned /64


def test_release_then_reuse(db_session):
    pool = _pool(db_session)
    a, b = _sub(db_session), _sub(db_session)
    pa = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=a.id)
    first = pa.prefix
    ipv6_pd.release_delegated_prefix(db_session, pa)
    assert pa.state == Ipv6PrefixState.available
    assert pa.subscriber_id is None
    pb = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=b.id)
    assert pb.prefix == first  # released prefix is reused


def test_active_prefix_cidr(db_session):
    pool = _pool(db_session)
    sub = _sub(db_session)
    ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
    assert (
        ipv6_pd.active_delegated_prefix_for_subscriber(db_session, sub.id)
        == "2001:db8::/64"
    )


def test_configurable_delegation_length(db_session):
    pool = _pool(db_session, cidr="2001:db8:abcd::/56", length=60)
    sub = _sub(db_session)
    pd = ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
    assert pd.prefix_length == 60
    assert pd.prefix == "2001:db8:abcd::"


def test_default_length_is_64_when_unset(db_session):
    pool = _pool(db_session, length=None)
    assert ipv6_pd.pool_delegation_length(pool) == 64


def test_release_subscriber_prefixes(db_session):
    pool = _pool(db_session)
    sub = _sub(db_session)
    ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
    n = ipv6_pd.release_subscriber_prefixes(db_session, sub.id)
    assert n == 1
    assert ipv6_pd.active_delegated_prefix_for_subscriber(db_session, sub.id) is None


def test_ipv4_pool_yields_no_prefixes(db_session):
    pool = _pool(db_session, cidr="10.0.0.0/24")
    sub = _sub(db_session)
    assert (
        ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
        is None
    )


def test_build_ipv6_pd_data_and_release(db_session):
    from app.services import web_network_ip as w

    pool = _pool(db_session)
    sub = _sub(db_session)
    ipv6_pd.allocate_delegated_prefix(db_session, pool=pool, subscriber_id=sub.id)
    db_session.commit()

    data = w.build_ipv6_pd_data(db_session)
    assert any(p["assigned"] == 1 for p in data["pd_pools"])
    row = next(r for r in data["pd_rows"] if r["cidr"] == "2001:db8::/64")
    assert row["state"] == "assigned"
    assert row["subscriber_name"]

    err = w.release_delegated_prefix_action(db_session, row["id"])
    assert err is None
    data2 = w.build_ipv6_pd_data(db_session)
    row2 = next(r for r in data2["pd_rows"] if r["cidr"] == "2001:db8::/64")
    assert row2["state"] == "available"
    assert row2["subscriber_name"] is None


def test_pool_form_persists_delegation_length(db_session):
    from app.services import web_network_ip as w

    values = w.parse_ip_pool_form(
        {
            "name": "PD Pool",
            "ip_version": "ipv6",
            "cidr": "2001:db8:abcd::/48",
            "delegation_prefix_length": "60",
        }
    )
    pool, err = w.create_ip_pool(db_session, values)
    assert err is None
    assert pool.delegation_prefix_length == 60


def test_pd_enabled_flag(monkeypatch):
    monkeypatch.delenv("IPV6_PD_ENABLED", raising=False)
    assert ipv6_pd.pd_enabled() is False
    monkeypatch.setenv("IPV6_PD_ENABLED", "true")
    assert ipv6_pd.pd_enabled() is True
    monkeypatch.setenv("IPV6_PD_ENABLED", "0")
    assert ipv6_pd.pd_enabled() is False


def test_radreply_emits_delegated_prefix():
    import types

    from app.models.catalog import SubscriptionStatus
    from app.services.radius_population import _radreply_attrs

    sub = types.SimpleNamespace(
        ipv4_address="10.0.0.5",
        ipv6_address=None,
        status=SubscriptionStatus.active,
        subscriber_id="sub-x",
    )
    attrs = _radreply_attrs(sub, None, None, delegated_ipv6="2001:db8::/64")
    assert ("Delegated-IPv6-Prefix", ":=", "2001:db8::/64") in attrs
    # no PD -> not emitted
    attrs2 = _radreply_attrs(sub, None, None)
    assert not [a for a in attrs2 if a[0] == "Delegated-IPv6-Prefix"]


def test_build_reply_emits_pd_only_when_flag_on(
    db_session, subscriber, catalog_offer, monkeypatch
):
    from app.services import connection_type_provisioning as ctp

    pool = _pool(db_session)
    ipv6_pd.allocate_delegated_prefix(
        db_session, pool=pool, subscriber_id=subscriber.id
    )

    from app.models.catalog import Subscription, SubscriptionStatus

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        login="1000777",
        ipv4_address="10.0.0.5",
    )
    db_session.add(subscription)
    db_session.commit()

    monkeypatch.setenv("IPV6_PD_ENABLED", "true")
    attrs = ctp.build_radius_reply_attributes(db_session, subscription)
    assert any(
        a["attribute"] == "Delegated-IPv6-Prefix" and a["value"] == "2001:db8::/64"
        for a in attrs
    )

    monkeypatch.setenv("IPV6_PD_ENABLED", "0")
    attrs_off = ctp.build_radius_reply_attributes(db_session, subscription)
    assert not [a for a in attrs_off if a["attribute"] == "Delegated-IPv6-Prefix"]
