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
