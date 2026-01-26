from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.provisioning import (
    InstallAppointment,
    ProvisioningTask,
    ServiceOrder,
    ServiceStateTransition,
)
from app.models.subscriber import Subscriber
from app.models.subscriber import AccountRole, SubscriberAccount


def _validate_account(db: Session, account_id: str) -> SubscriberAccount:
    account = db.get(SubscriberAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Subscriber account not found")
    return account


def validate_service_order_links(
    db: Session,
    account_id: str,
    subscription_id: str | None,
    requested_by_contact_id: str | None,
):
    _validate_account(db, account_id)

    if subscription_id:
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if str(subscription.account_id) != account_id:
            raise HTTPException(
                status_code=400, detail="Subscription does not belong to account"
            )

    if requested_by_contact_id:
        subscriber = db.get(Subscriber, requested_by_contact_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Contact not found")
        linked = (
            db.query(AccountRole)
            .filter(AccountRole.account_id == account_id)
            .filter(AccountRole.subscriber_id == subscriber.id)
            .first()
        )
        if not linked:
            raise HTTPException(
                status_code=400, detail="Contact does not belong to account"
            )


def validate_service_order_exists(db: Session, service_order_id: str) -> ServiceOrder:
    service_order = db.get(ServiceOrder, service_order_id)
    if not service_order:
        raise HTTPException(status_code=404, detail="Service order not found")
    return service_order


def validate_install_appointment_links(db: Session, service_order_id: str) -> InstallAppointment | None:
    validate_service_order_exists(db, service_order_id)
    return None


def validate_provisioning_task_links(db: Session, service_order_id: str) -> ProvisioningTask | None:
    validate_service_order_exists(db, service_order_id)
    return None


def validate_state_transition_links(db: Session, service_order_id: str) -> ServiceStateTransition | None:
    validate_service_order_exists(db, service_order_id)
    return None
