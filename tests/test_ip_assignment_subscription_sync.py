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
from app.models.network import IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.schemas.network import IPAssignmentCreate
from app.services import network as network_service


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
