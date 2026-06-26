"""Tests for the create-on-demand IPv4 allocator.

Closes the display/allocate asymmetry: a pool whose ``ipv4_addresses`` rows were
never materialized shows IPs "free" (the availability view computes hosts from
the CIDR) but allocation could only hand out existing rows. See
app/services/provisioning_helpers.py:_allocate_ipv4_on_demand.
"""

from __future__ import annotations

import uuid

from app.models.network import (
    IPAssignment,
    IpBlock,
    IpPool,
    IPv4Address,
    IPVersion,
    SubscriberAdditionalRoute,
)
from app.models.subscriber import Subscriber
from app.services.provisioning_helpers import _allocate_ipv4_on_demand


def _subscriber(db):
    s = Subscriber(first_name="A", last_name="L", email=f"{uuid.uuid4().hex[:8]}@e.com")
    db.add(s)
    db.flush()
    return s


def _pool(db, cidr="10.0.0.0/24", notes=None):
    pool = IpPool(
        id=uuid.uuid4(),
        name=f"pool-{uuid.uuid4().hex[:6]}",
        ip_version=IPVersion.ipv4,
        cidr=cidr,
        is_active=True,
        notes=notes,
    )
    db.add(pool)
    db.flush()
    return pool


def test_materializes_lowest_free_host_from_block(db_session):
    pool = _pool(db_session, cidr="10.0.0.0/24")
    db_session.add(IpBlock(pool_id=pool.id, cidr="10.0.5.0/30", is_active=True))
    db_session.flush()

    addr = _allocate_ipv4_on_demand(db_session, pool)

    # Block ranges win over pool.cidr; /30 default skips network/broadcast.
    assert addr is not None
    assert addr.address == "10.0.5.1"
    assert addr.pool_id == pool.id


def test_falls_back_to_pool_cidr_when_no_blocks(db_session):
    pool = _pool(db_session, cidr="10.1.0.0/24")
    addr = _allocate_ipv4_on_demand(db_session, pool)
    assert addr is not None
    assert addr.address == "10.1.0.1"  # lowest host of the pool CIDR


def test_skips_reserved_management_ont_and_assigned(db_session):
    pool = _pool(db_session, cidr="10.2.0.0/24")
    # .1 reserved, .2 management, .3 actively assigned to someone
    db_session.add_all(
        [
            IPv4Address(address="10.2.0.1", pool_id=pool.id, is_reserved=True),
            IPv4Address(
                address="10.2.0.2", pool_id=pool.id, allocation_type="management"
            ),
        ]
    )
    taken = IPv4Address(address="10.2.0.3", pool_id=pool.id)
    db_session.add(taken)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=_subscriber(db_session).id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=taken.id,
            is_active=True,
        )
    )
    db_session.flush()

    addr = _allocate_ipv4_on_demand(db_session, pool)
    assert addr is not None
    assert addr.address == "10.2.0.4"  # first IP past the unsafe/taken ones


def test_reuses_address_with_only_inactive_assignment(db_session):
    """An inactive assignment means the IP was *released* (suspended customers
    keep ACTIVE assignments), so the allocator reuses it — the H1 fix for the
    asymmetric-release pool-exhaustion bug."""
    pool = _pool(db_session, cidr="10.3.0.0/24")
    released = IPv4Address(address="10.3.0.1", pool_id=pool.id)
    db_session.add(released)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=_subscriber(db_session).id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=released.id,
            is_active=False,
        )
    )
    db_session.flush()

    addr = _allocate_ipv4_on_demand(db_session, pool)
    assert addr.address == "10.3.0.1"  # released IP is re-handed-out, not skipped


def test_skips_address_with_active_assignment(db_session):
    """An ACTIVE assignment (live/suspended customer) still blocks reuse."""
    pool = _pool(db_session, cidr="10.4.0.0/24")
    held = IPv4Address(address="10.4.0.1", pool_id=pool.id)
    db_session.add(held)
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=_subscriber(db_session).id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=held.id,
            is_active=True,
        )
    )
    db_session.flush()

    addr = _allocate_ipv4_on_demand(db_session, pool)
    assert addr.address == "10.4.0.2"


def test_reuses_and_attaches_loose_row(db_session):
    """A safe unassigned row with pool_id IS NULL is reused, not duplicated."""
    pool = _pool(db_session, cidr="10.4.0.0/24")
    loose = IPv4Address(address="10.4.0.1", pool_id=None, allocation_type="wan")
    db_session.add(loose)
    db_session.flush()

    addr = _allocate_ipv4_on_demand(db_session, pool)
    assert addr.id == loose.id  # reused, not a new row
    assert addr.pool_id == pool.id  # attached to the pool


def test_skips_active_routed_block_hosts(db_session):
    """Hosts inside an active SubscriberAdditionalRoute belong to a customer via
    Framed-Route (no IPAssignment), so the allocator must not hand them out."""
    pool = _pool(db_session, cidr="10.6.0.0/24")
    db_session.add(IpBlock(pool_id=pool.id, cidr="10.6.0.0/29", is_active=True))
    sub = _subscriber(db_session)
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=sub.id,
            cidr="10.6.0.0/30",
            prefix_length=30,
            metric=1,
            is_active=True,
        )
    )
    db_session.flush()

    addr = _allocate_ipv4_on_demand(db_session, pool)
    # /29 hosts are .1–.6; .1/.2/.3 all fall inside the routed 10.6.0.0/30
    # (membership includes its network/broadcast) → first free host is .4.
    assert addr.address == "10.6.0.4"


def test_allow_network_broadcast_includes_network_address(db_session):
    pool = _pool(db_session, cidr="10.5.0.0/30", notes="[allow_network_broadcast:true]")
    addr = _allocate_ipv4_on_demand(db_session, pool)
    assert addr.address == "10.5.0.0"  # network address usable when opted in


def test_reactivation_address_validity_guard(db_session):
    """A released address that has since become reserved/management or been
    swallowed by an active routed block must not be reactivated in place."""
    from app.services.provisioning_helpers import _reactivation_address_is_valid

    pool = _pool(db_session, cidr="10.7.0.0/24")
    ok = IPv4Address(address="10.7.0.10", pool_id=pool.id)
    reserved = IPv4Address(address="10.7.0.11", pool_id=pool.id, is_reserved=True)
    mgmt = IPv4Address(
        address="10.7.0.12", pool_id=pool.id, allocation_type="management"
    )
    routed = IPv4Address(address="10.7.0.20", pool_id=pool.id)
    db_session.add_all([ok, reserved, mgmt, routed])
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=_subscriber(db_session).id,
            cidr="10.7.0.20/30",
            prefix_length=30,
            metric=1,
            is_active=True,
        )
    )
    db_session.flush()

    assert _reactivation_address_is_valid(db_session, ok) is True
    assert _reactivation_address_is_valid(db_session, reserved) is False
    assert _reactivation_address_is_valid(db_session, mgmt) is False
    assert _reactivation_address_is_valid(db_session, routed) is False
    assert _reactivation_address_is_valid(db_session, None) is False


def test_find_available_address_reuses_released_address(db_session):
    """The primary allocator path now returns an address whose only assignment is
    inactive (released) — the read side of the asymmetric-release fix."""
    from app.services.provisioning_helpers import _find_available_address

    pool = _pool(db_session, cidr="10.8.0.0/24")
    a1 = IPv4Address(address="10.8.0.1", pool_id=pool.id)
    a2 = IPv4Address(address="10.8.0.2", pool_id=pool.id)
    db_session.add_all([a1, a2])
    db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=_subscriber(db_session).id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=a1.id,
            is_active=False,
        )
    )
    db_session.flush()

    addr = _find_available_address(db_session, IPVersion.ipv4, str(pool.id))
    assert addr is not None
    assert addr.address == "10.8.0.1"  # released address, not skipped to .2


def test_partial_unique_allows_inactive_plus_active_on_same_address(db_session):
    """Partial-unique index: a released (inactive) row may coexist with a fresh
    active row on the same address; the relationship prefers the active one."""
    pool = _pool(db_session, cidr="10.9.0.0/24")
    addr = IPv4Address(address="10.9.0.1", pool_id=pool.id)
    db_session.add(addr)
    db_session.flush()
    db_session.add_all(
        [
            IPAssignment(
                subscriber_id=_subscriber(db_session).id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=addr.id,
                is_active=False,
            ),
            IPAssignment(
                subscriber_id=_subscriber(db_session).id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=addr.id,
                is_active=True,
            ),
        ]
    )
    db_session.flush()  # must NOT raise — only the active row is in the unique index

    db_session.refresh(addr)
    assert addr.assignment is not None and addr.assignment.is_active is True
