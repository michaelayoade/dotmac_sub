from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionAddOn
from app.models.network import CPEDevice, IPAssignment
from app.models.subscriber import Address, SubscriberAccount


def _validate_account(db: Session, account_id: str) -> SubscriberAccount:
    account = db.get(SubscriberAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Subscriber account not found")
    return account


def _validate_address_belongs(db: Session, account: SubscriberAccount, address_id: str):
    address = db.get(Address, address_id)
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")
    if address.subscriber_id != account.subscriber_id:
        raise HTTPException(
            status_code=400, detail="Service address does not belong to subscriber"
        )
    if address.account_id and address.account_id != account.id:
        raise HTTPException(
            status_code=400, detail="Service address does not belong to account"
        )


def validate_cpe_device_links(
    db: Session,
    account_id: str,
    subscription_id: str | None,
    service_address_id: str | None,
):
    account = _validate_account(db, account_id)
    if subscription_id:
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if str(subscription.account_id) != account_id:
            raise HTTPException(status_code=400, detail="Subscription does not belong to account")
    if service_address_id:
        _validate_address_belongs(db, account, service_address_id)


def validate_ip_assignment_links(
    db: Session,
    account_id: str,
    subscription_id: str | None,
    subscription_add_on_id: str | None,
    service_address_id: str | None,
):
    account = _validate_account(db, account_id)
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
        if str(subscription.account_id) != account_id:
            raise HTTPException(status_code=400, detail="Subscription does not belong to account")

    if service_address_id:
        _validate_address_belongs(db, account, service_address_id)


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
