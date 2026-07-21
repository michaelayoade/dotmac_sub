from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.project import Project, ProjectTask
from app.models.provisioning import (
    InstallAppointment,
    ProvisioningTask,
    ServiceOrder,
    ServiceStateTransition,
)
from app.models.subscriber import Subscriber


def _validate_subscriber(db: Session, subscriber_id: str) -> Subscriber:
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return subscriber


def validate_service_order_links(
    db: Session,
    subscriber_id: str,
    subscription_id: str | None,
    requested_by_contact_id: str | None,
    project_id: str | None = None,
    activation_project_task_id: str | None = None,
):
    _validate_subscriber(db, subscriber_id)

    if subscription_id:
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if str(subscription.subscriber_id) != subscriber_id:
            raise HTTPException(
                status_code=400, detail="Subscription does not belong to subscriber"
            )

    if requested_by_contact_id:
        contact = db.get(Subscriber, requested_by_contact_id)
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")

    project = None
    if project_id:
        project = db.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project.subscriber_id and str(project.subscriber_id) != subscriber_id:
            raise HTTPException(
                status_code=400, detail="Project does not belong to subscriber"
            )

    if activation_project_task_id:
        task = db.get(ProjectTask, activation_project_task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Project task not found")
        if project is None:
            raise HTTPException(
                status_code=400,
                detail="Activation project task requires a project binding",
            )
        if task.project_id != project.id:
            raise HTTPException(
                status_code=400,
                detail="Activation project task does not belong to project",
            )


def validate_service_order_exists(db: Session, service_order_id: str) -> ServiceOrder:
    service_order = db.get(ServiceOrder, service_order_id)
    if not service_order:
        raise HTTPException(status_code=404, detail="Service order not found")
    return service_order


def validate_install_appointment_links(
    db: Session, service_order_id: str
) -> InstallAppointment | None:
    validate_service_order_exists(db, service_order_id)
    return None


def validate_provisioning_task_links(
    db: Session, service_order_id: str
) -> ProvisioningTask | None:
    validate_service_order_exists(db, service_order_id)
    return None


def validate_state_transition_links(
    db: Session, service_order_id: str
) -> ServiceStateTransition | None:
    validate_service_order_exists(db, service_order_id)
    return None
