"""Allocator must not reissue an IPv4 still used by another live service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.catalog import SubscriptionStatus
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.radius_active_session import RadiusActiveSession
from app.schemas.catalog import SubscriptionCreate
from app.services import catalog as catalog_service
from app.services.provisioning_helpers import (
    _allocate_ipv4_on_demand,
    _ensure_ip_assignment_for_version,
    _find_available_address,
)


def _pool(db, cidr="10.94.0.0/29"):
    pool = IpPool(
        name=f"pool-{uuid4().hex[:6]}",
        ip_version=IPVersion.ipv4,
        cidr=cidr,
        is_active=True,
    )
    db.add(pool)
    db.flush()
    return pool


def _rows(db, pool, *octets):
    rows = [
        IPv4Address(address=f"10.94.0.{o}", pool_id=pool.id, allocation_type="static")
        for o in octets
    ]
    db.add_all(rows)
    db.flush()
    return rows


def _second_subscription(db, subscription):
    return catalog_service.subscriptions.create(
        db,
        SubscriptionCreate(
            account_id=subscription.subscriber_id,
            offer_id=subscription.offer_id,
            # Created in a non-"active" state to pass the single-active
            # validator; tests set the status they need (legacy data has
            # several live services per account).
            status=SubscriptionStatus.stopped,
        ),
    )


def _session(db, subscription, ip):
    db.add(
        RadiusActiveSession(
            subscription_id=subscription.id,
            subscriber_id=subscription.subscriber_id,
            username=f"u-{uuid4().hex[:6]}",
            acct_session_id=uuid4().hex,
            framed_ip_address=ip,
            session_start=datetime.now(UTC),
        )
    )
    db.flush()


def test_find_available_skips_other_subscription_served_column(
    db_session, subscription
):
    pool = _pool(db_session)
    _rows(db_session, pool, 1, 2)
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.active
    other.ipv4_address = "10.94.0.1"  # IPAM shows .1 free; column says taken
    db_session.flush()

    addr = _find_available_address(
        db_session, IPVersion.ipv4, str(pool.id), subscription_id=subscription.id
    )
    assert addr is not None and addr.address == "10.94.0.2"


def test_find_available_skips_live_session_ip(db_session, subscription):
    pool = _pool(db_session)
    _rows(db_session, pool, 1, 2)
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.active
    _session(db_session, other, "10.94.0.1")

    addr = _find_available_address(
        db_session, IPVersion.ipv4, str(pool.id), subscription_id=subscription.id
    )
    assert addr is not None and addr.address == "10.94.0.2"


def test_terminal_subscription_column_does_not_block_and_own_column_is_ignored(
    db_session, subscription
):
    pool = _pool(db_session)
    _rows(db_session, pool, 1, 2)
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.canceled
    other.ipv4_address = "10.94.0.1"
    subscription.ipv4_address = "10.94.0.2"
    db_session.flush()

    addr = _find_available_address(
        db_session, IPVersion.ipv4, str(pool.id), subscription_id=subscription.id
    )
    assert addr is not None and addr.address == "10.94.0.1"


def test_on_demand_skips_column_and_session_ips(db_session, subscription):
    pool = _pool(db_session)  # /29 hosts .1-.6, no rows materialized
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.active
    other.ipv4_address = "10.94.0.1"
    _session(db_session, other, "10.94.0.2")

    addr = _allocate_ipv4_on_demand(db_session, pool, subscription_id=subscription.id)
    assert addr is not None and addr.address == "10.94.0.3"


def test_new_subscription_does_not_inherit_sibling_assignment(db_session, subscription):
    pool = _pool(db_session)
    sibling_ip, free_ip = _rows(db_session, pool, 1, 2)
    sibling = _second_subscription(db_session, subscription)
    sibling.status = SubscriptionStatus.active
    db_session.add(
        IPAssignment(
            subscriber_id=subscription.subscriber_id,
            subscription_id=sibling.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=sibling_ip.id,
            is_active=True,
        )
    )
    db_session.flush()

    assignment, address = _ensure_ip_assignment_for_version(
        db_session, subscription, IPVersion.ipv4, {}
    )
    assert address is not None and address.address == "10.94.0.2"
    assert assignment is not None and assignment.subscription_id == subscription.id
    assert subscription.ipv4_address == "10.94.0.2"


def test_exact_subscription_assignment_is_picked_among_siblings(
    db_session, subscription
):
    pool = _pool(db_session)
    a, b = _rows(db_session, pool, 1, 2)
    sibling = _second_subscription(db_session, subscription)
    sibling.status = SubscriptionStatus.active
    for sub, addr in ((sibling, a), (subscription, b)):  # sibling's sorts first
        db_session.add(
            IPAssignment(
                subscriber_id=subscription.subscriber_id,
                subscription_id=sub.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=addr.id,
                is_active=True,
            )
        )
    db_session.flush()

    _, address = _ensure_ip_assignment_for_version(
        db_session, subscription, IPVersion.ipv4, {}
    )
    assert address is not None and address.address == "10.94.0.2"


def test_stored_slash32_served_column_blocks_pool_scan(db_session, subscription):
    pool = _pool(db_session)
    _rows(db_session, pool, 1, 2)
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.active
    other.ipv4_address = "10.94.0.1/32"
    db_session.flush()

    addr = _find_available_address(
        db_session, IPVersion.ipv4, str(pool.id), subscription_id=subscription.id
    )
    assert addr is not None and addr.address == "10.94.0.2"


def test_requested_ip_held_by_other_live_service_is_refused(db_session, subscription):
    import pytest
    from fastapi import HTTPException

    pool = _pool(db_session)
    _rows(db_session, pool, 1, 2)
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.active
    other.ipv4_address = "10.94.0.1/32"
    db_session.flush()

    with pytest.raises(HTTPException) as excinfo:
        _ensure_ip_assignment_for_version(
            db_session, subscription, IPVersion.ipv4, {"ipv4_address": "10.94.0.1"}
        )
    assert excinfo.value.status_code == 409


def test_stored_ip_held_by_live_session_of_other_service_is_refused(
    db_session, subscription
):
    import pytest
    from fastapi import HTTPException

    pool = _pool(db_session)
    _rows(db_session, pool, 1)
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.active
    _session(db_session, other, "10.94.0.1")
    subscription.ipv4_address = "10.94.0.1"
    db_session.flush()

    with pytest.raises(HTTPException):
        _ensure_ip_assignment_for_version(db_session, subscription, IPVersion.ipv4, {})


def test_reactivation_skips_address_held_by_other_live_service(
    db_session, subscription
):
    pool = _pool(db_session)
    old, fresh = _rows(db_session, pool, 1, 2)
    db_session.add(
        IPAssignment(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=old.id,
            is_active=False,
        )
    )
    other = _second_subscription(db_session, subscription)
    other.status = SubscriptionStatus.active
    other.ipv4_address = "10.94.0.1/32"
    db_session.flush()

    _, address = _ensure_ip_assignment_for_version(
        db_session, subscription, IPVersion.ipv4, {}
    )
    assert address is not None and address.address == "10.94.0.2"


def test_reactivation_keeps_own_address_when_only_own_session_uses_it(
    db_session, subscription
):
    pool = _pool(db_session)
    (old,) = _rows(db_session, pool, 1)
    db_session.add(
        IPAssignment(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=old.id,
            is_active=False,
        )
    )
    _session(db_session, subscription, "10.94.0.1")

    _, address = _ensure_ip_assignment_for_version(
        db_session, subscription, IPVersion.ipv4, {}
    )
    assert address is not None and address.address == "10.94.0.1"
