from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionAddOn
from app.models.network import CPEDevice, IPAssignment
from app.models.subscriber import Address, Subscriber


def _validate_subscriber(db: Session, subscriber_id: str) -> Subscriber:
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return subscriber


def _validate_address_belongs(db: Session, subscriber: Subscriber, address_id: str):
    address = db.get(Address, address_id)
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    if address.subscriber_id != subscriber.id:
        raise HTTPException(
            status_code=400, detail="Service address does not belong to subscriber"
        )


def validate_cpe_device_links(
    db: Session,
    subscriber_id: str,
    subscription_id: str | None,
    service_address_id: str | None,
):
    subscriber = _validate_subscriber(db, subscriber_id)
    if subscription_id:
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if str(subscription.subscriber_id) != subscriber_id:
            raise HTTPException(status_code=400, detail="Subscription does not belong to subscriber")
    if service_address_id:
        _validate_address_belongs(db, subscriber, service_address_id)


def validate_ip_assignment_links(
    db: Session,
    subscriber_id: str,
    subscription_id: str | None,
    subscription_add_on_id: str | None,
    service_address_id: str | None,
):
    subscriber = _validate_subscriber(db, subscriber_id)
    derived_subscription_id = subscription_id

    if subscription_add_on_id:
        sub_add_on = db.get(SubscriptionAddOn, subscription_add_on_id)
        if not sub_add_on:
            raise HTTPException(
                status_code=404, detail="Subscription add-on not found"
            )
        if subscription_id and str(sub_add_on.subscription_id) != subscription_id:
            raise HTTPException(
                status_code=400,
                detail="Subscription add-on does not belong to subscription",
            )
        derived_subscription_id = str(sub_add_on.subscription_id)

    if derived_subscription_id:
        subscription = db.get(Subscription, derived_subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if str(subscription.subscriber_id) != subscriber_id:
            raise HTTPException(status_code=400, detail="Subscription does not belong to subscriber")

    if service_address_id:
        _validate_address_belongs(db, subscriber, service_address_id)


def validate_cpe_device_exists(db: Session, device_id: str) -> CPEDevice:
    device = db.get(CPEDevice, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="CPE device not found")
    return device


def validate_ip_assignment_exists(db: Session, assignment_id: str) -> IPAssignment:
    assignment = db.get(IPAssignment, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="IP assignment not found")
    return assignment
