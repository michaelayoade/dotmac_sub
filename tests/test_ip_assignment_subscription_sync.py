import uuid

from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.schemas.network import IPAssignmentCreate
from app.services import network as network_service
from app.services import web_network_ip as ip_service


def _make_subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="IPAM",
        last_name="Owner",
        email=f"ipam-owner-{uuid.uuid4().hex[:8]}@example.com",
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _make_offer(db_session) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"IPAM Sync Offer {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    return offer


def _make_subscription(db_session, subscriber: Subscriber) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=_make_offer(db_session).id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _make_ipv4(
    db_session, address: str, *, allocation_type: str | None = None
) -> IPv4Address:
    record = IPv4Address(address=address, allocation_type=allocation_type)
    db_session.add(record)
    db_session.flush()
    return record


def _make_pool(db_session, cidr: str) -> IpPool:
    pool = IpPool(
        name=f"IPAM Pool {uuid.uuid4().hex[:8]}",
        ip_version=IPVersion.ipv4,
        cidr=cidr,
    )
    db_session.add(pool)
    db_session.flush()
    return pool


def test_ip_assignment_with_subscription_syncs_active_subscription_ipv4(db_session):
    subscriber = _make_subscriber(db_session)
    subscription = _make_subscription(db_session, subscriber)
    address = _make_ipv4(db_session, "100.64.10.5")

    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    db_session.refresh(subscription)
    assert subscription.ipv4_address == "100.64.10.5"


def test_ip_assignment_release_clears_matching_subscription_ipv4(db_session):
    subscriber = _make_subscriber(db_session)
    subscription = _make_subscription(db_session, subscriber)
    address = _make_ipv4(db_session, "100.64.10.6", allocation_type="wan")
    assignment = network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    network_service.ip_assignments.delete(db_session, str(assignment.id))

    db_session.refresh(subscription)
    db_session.refresh(address)
    assert subscription.ipv4_address is None
    assert address.allocation_type is None


def test_ip_assignment_release_preserves_management_allocation_type(db_session):
    subscriber = _make_subscriber(db_session)
    subscription = _make_subscription(db_session, subscriber)
    address = _make_ipv4(db_session, "100.64.10.60", allocation_type="management")
    assignment = network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    network_service.ip_assignments.delete(db_session, str(assignment.id))

    db_session.refresh(address)
    assert address.allocation_type == "management"


def test_subscriber_level_assignment_does_not_guess_with_multiple_active_services(
    db_session,
):
    subscriber = _make_subscriber(db_session)
    first = _make_subscription(db_session, subscriber)
    second = _make_subscription(db_session, subscriber)
    address = _make_ipv4(db_session, "100.64.10.7")

    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            subscriber_id=subscriber.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
        ),
    )

    db_session.refresh(first)
    db_session.refresh(second)
    assert first.ipv4_address is None
    assert second.ipv4_address is None


def test_assign_ipv4_address_reports_previous_owner_on_reassign(db_session):
    pool = _make_pool(db_session, "100.64.20.0/24")
    old = _make_subscriber(db_session)
    old_sub = _make_subscription(db_session, old)
    new = _make_subscriber(db_session)
    address = _make_ipv4(db_session, "100.64.20.5")
    address.pool_id = pool.id
    db_session.flush()

    ip_service.assign_ipv4_address(
        db_session,
        pool_id=str(pool.id),
        ip_address="100.64.20.5",
        subscriber_id=str(old.id),
        subscription_id=str(old_sub.id),
    )

    result = ip_service.assign_ipv4_address(
        db_session,
        pool_id=str(pool.id),
        ip_address="100.64.20.5",
        subscriber_id=str(new.id),
    )

    assert result["reassigned"] is True
    assert result["previous_subscriber_id"] == str(old.id)
    assert result["previous_subscription_id"] == str(old_sub.id)
    assert str(result["assignment"].subscriber_id) == str(new.id)


def test_reassign_after_release_reactivates_same_row(db_session):
    # The hard unique constraint uq_ip_assignments_ipv4_address_id forbids a
    # second row for the same address. Releasing soft-deletes the row, so a
    # later assignment must reactivate it in place rather than insert a
    # colliding row. Guards the Release/Unassign flow against a wedge.
    pool = _make_pool(db_session, "100.64.21.0/24")
    old = _make_subscriber(db_session)
    new = _make_subscriber(db_session)
    address = _make_ipv4(db_session, "100.64.21.9")
    address.pool_id = pool.id
    db_session.flush()

    first = ip_service.assign_ipv4_address(
        db_session,
        pool_id=str(pool.id),
        ip_address="100.64.21.9",
        subscriber_id=str(old.id),
    )
    first_id = first["assignment"].id

    network_service.ip_assignments.delete(db_session, str(first_id))

    second = ip_service.assign_ipv4_address(
        db_session,
        pool_id=str(pool.id),
        ip_address="100.64.21.9",
        subscriber_id=str(new.id),
    )

    assert second["assignment"].id == first_id
    assert second["assignment"].is_active is True
    assert str(second["assignment"].subscriber_id) == str(new.id)

    rows = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.ipv4_address_id == address.id)
        .all()
    )
    assert len(rows) == 1


def test_bulk_assign_ipv4_resolves_owner_and_pool(db_session):
    from app.services import web_network_ip_actions as actions

    pool = _make_pool(db_session, "100.64.30.0/24")
    by_id = _make_subscriber(db_session)
    by_account = _make_subscriber(db_session)
    by_account.account_number = "ACC-BULK-1"
    by_email = _make_subscriber(db_session)
    db_session.flush()
    for octet in (10, 11, 12):
        ip = IPv4Address(address=f"100.64.30.{octet}", pool_id=pool.id)
        db_session.add(ip)
    db_session.flush()

    rows = [
        {"ip_address": "100.64.30.10", "subscriber": str(by_id.id)},
        {"ip_address": "100.64.30.11", "subscriber": "ACC-BULK-1"},
        {"ip_address": "100.64.30.12", "subscriber": by_email.email.upper()},
    ]
    summary = actions.bulk_assign_ipv4(db_session, rows)

    assert summary["assigned"] == 3
    assert summary["reassigned"] == 0
    assert summary["errors"] == []
    assert len(summary["audit"]) == 3


def test_bulk_assign_ipv4_isolates_bad_rows(db_session):
    from app.services import web_network_ip_actions as actions

    pool = _make_pool(db_session, "100.64.31.0/24")
    good = _make_subscriber(db_session)
    db_session.add(IPv4Address(address="100.64.31.5", pool_id=pool.id))
    db_session.flush()

    rows = [
        {"ip_address": "100.64.31.5", "subscriber": str(good.id)},  # ok
        {"ip_address": "100.64.31.6", "subscriber": "nobody@nowhere"},  # bad subscriber
        {"ip_address": "10.255.255.1", "subscriber": str(good.id)},  # no pool
        {"ip_address": "", "subscriber": str(good.id)},  # missing ip
    ]
    summary = actions.bulk_assign_ipv4(db_session, rows)

    assert summary["assigned"] == 1
    assert summary["total_rows"] == 4
    assert len(summary["errors"]) == 3
    error_msgs = " ".join(e["error"] for e in summary["errors"])
    assert "Subscriber not found" in error_msgs
    assert "No active IPv4 pool" in error_msgs
    assert "required" in error_msgs


def test_ip_pool_utilization_snapshot_counts(db_session):
    from app.models.network import IpPoolUtilizationSnapshot
    from app.services.ip_pool_utilization_snapshot import (
        ip_pool_utilization_snapshots,
    )

    pool = _make_pool(db_session, "100.64.40.0/24")
    s1 = _make_subscriber(db_session)
    s2 = _make_subscriber(db_session)
    a1 = IPv4Address(address="100.64.40.10", pool_id=pool.id)
    a2 = IPv4Address(address="100.64.40.11", pool_id=pool.id)
    a3 = IPv4Address(address="100.64.40.12", pool_id=pool.id, is_reserved=True)
    db_session.add_all([a1, a2, a3])
    db_session.flush()
    for subscriber, address in ((s1, a1), (s2, a2)):
        network_service.ip_assignments.create(
            db_session,
            IPAssignmentCreate(
                subscriber_id=subscriber.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=address.id,
            ),
        )

    result = ip_pool_utilization_snapshots.take_snapshot(db_session)
    assert result["created"] >= 1

    snap = (
        db_session.query(IpPoolUtilizationSnapshot)
        .filter(IpPoolUtilizationSnapshot.pool_id == pool.id)
        .one()
    )
    assert snap.used == 2
    assert snap.reserved == 1
    assert snap.total == 254  # /24 minus network + broadcast
    assert snap.available == 254 - 2 - 1
    assert snap.percent == round(2 / 254 * 100)

    history = ip_pool_utilization_snapshots.history(db_session, str(pool.id))
    assert len(history) == 1


def test_prune_ip_pool_utilization_snapshots(db_session):
    from datetime import UTC, datetime, timedelta

    from app.models.network import IpPoolUtilizationSnapshot
    from app.services.ip_pool_utilization_snapshot import (
        ip_pool_utilization_snapshots,
    )

    pool = _make_pool(db_session, "100.64.50.0/24")
    now = datetime.now(UTC)
    old = IpPoolUtilizationSnapshot(
        pool_id=pool.id,
        captured_at=now - timedelta(days=500),
        total=254,
        used=10,
        reserved=0,
        available=244,
        percent=4,
    )
    recent = IpPoolUtilizationSnapshot(
        pool_id=pool.id,
        captured_at=now - timedelta(days=10),
        total=254,
        used=12,
        reserved=0,
        available=242,
        percent=5,
    )
    db_session.add_all([old, recent])
    db_session.flush()
    recent_id = recent.id

    result = ip_pool_utilization_snapshots.prune(db_session, keep_days=400)
    assert result["deleted"] == 1

    remaining = (
        db_session.query(IpPoolUtilizationSnapshot)
        .filter(IpPoolUtilizationSnapshot.pool_id == pool.id)
        .all()
    )
    assert len(remaining) == 1
    assert remaining[0].id == recent_id
