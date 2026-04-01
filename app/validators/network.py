from fastapi import HTTPException
from sqlalchemy.orm import Session

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
    service_address_id: str | None,
):
    """Validate CPE device link constraints.

    Devices link directly to subscribers (not subscriptions) for independent
    OLT management.
    """
    subscriber = _validate_subscriber(db, subscriber_id)
    if service_address_id:
        _validate_address_belongs(db, subscriber, service_address_id)


def validate_ip_assignment_links(
    db: Session,
    subscriber_id: str,
    service_address_id: str | None,
):
    """Validate IP assignment link constraints.

    IP assignments link directly to subscribers (not subscriptions) for independent
    OLT management.
    """
    subscriber = _validate_subscriber(db, subscriber_id)
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
