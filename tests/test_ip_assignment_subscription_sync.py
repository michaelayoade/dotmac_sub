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


def _make_ipv4(db_session, address: str) -> IPv4Address:
    record = IPv4Address(address=address)
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
    address = _make_ipv4(db_session, "100.64.10.6")
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
    assert subscription.ipv4_address is None


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
