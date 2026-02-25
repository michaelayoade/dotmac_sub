from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    InstallAppointmentRead,
    InstallAppointmentUpdate,
    ProvisioningRunCreate,
    ProvisioningRunRead,
    ProvisioningRunStart,
    ProvisioningRunUpdate,
    ProvisioningStepCreate,
    ProvisioningStepRead,
    ProvisioningStepUpdate,
    ProvisioningTaskCreate,
    ProvisioningTaskRead,
    ProvisioningTaskUpdate,
    ProvisioningWorkflowCreate,
    ProvisioningWorkflowRead,
    ProvisioningWorkflowUpdate,
    ServiceOrderCreate,
    ServiceOrderRead,
    ServiceOrderUpdate,
    ServiceStateTransitionCreate,
    ServiceStateTransitionRead,
)
from app.services import provisioning as provisioning_service
from app.services.auth_dependencies import require_permission

router = APIRouter()


@router.get(
    "/service-orders",
    response_model=ListResponse[ServiceOrderRead],
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_service_orders(
    subscriber_id: str | None = None,
    account_id: str | None = None,
    subscription_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if not subscriber_id and account_id:
        subscriber_id = account_id
    return provisioning_service.service_orders.list_response(
        db, subscriber_id, subscription_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/service-orders",
    response_model=ServiceOrderRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_service_order(payload: ServiceOrderCreate, db: Session = Depends(get_db)):
    return provisioning_service.service_orders.create(db, payload)


@router.get(
    "/service-orders/{order_id}",
    response_model=ServiceOrderRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_service_order(order_id: str, db: Session = Depends(get_db)):
    return provisioning_service.service_orders.get(db, order_id)


@router.patch(
    "/service-orders/{order_id}",
    response_model=ServiceOrderRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_service_order(
    order_id: str, payload: ServiceOrderUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.service_orders.update(db, order_id, payload)


@router.delete(
    "/service-orders/{order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_service_order(order_id: str, db: Session = Depends(get_db)):
    provisioning_service.service_orders.delete(db, order_id)


@router.get(
    "/install-appointments",
    response_model=ListResponse[InstallAppointmentRead],
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_install_appointments(
    service_order_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="scheduled_start"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.install_appointments.list_response(
        db, service_order_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/install-appointments",
    response_model=InstallAppointmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_install_appointment(
    payload: InstallAppointmentCreate, db: Session = Depends(get_db)
):
    return provisioning_service.install_appointments.create(db, payload)


@router.get(
    "/install-appointments/{appointment_id}",
    response_model=InstallAppointmentRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_install_appointment(appointment_id: str, db: Session = Depends(get_db)):
    return provisioning_service.install_appointments.get(db, appointment_id)


@router.patch(
    "/install-appointments/{appointment_id}",
    response_model=InstallAppointmentRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_install_appointment(
    appointment_id: str,
    payload: InstallAppointmentUpdate,
    db: Session = Depends(get_db),
):
    return provisioning_service.install_appointments.update(db, appointment_id, payload)


@router.delete(
    "/install-appointments/{appointment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_install_appointment(appointment_id: str, db: Session = Depends(get_db)):
    provisioning_service.install_appointments.delete(db, appointment_id)


@router.get(
    "/provisioning-tasks",
    response_model=ListResponse[ProvisioningTaskRead],
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_provisioning_tasks(
    service_order_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_tasks.list_response(
        db, service_order_id, status, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-tasks",
    response_model=ProvisioningTaskRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_provisioning_task(
    payload: ProvisioningTaskCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_tasks.create(db, payload)


@router.get(
    "/provisioning-tasks/{task_id}",
    response_model=ProvisioningTaskRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_provisioning_task(task_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_tasks.get(db, task_id)


@router.patch(
    "/provisioning-tasks/{task_id}",
    response_model=ProvisioningTaskRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_provisioning_task(
    task_id: str, payload: ProvisioningTaskUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_tasks.update(db, task_id, payload)


@router.delete(
    "/provisioning-tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_provisioning_task(task_id: str, db: Session = Depends(get_db)):
    provisioning_service.provisioning_tasks.delete(db, task_id)


@router.get(
    "/provisioning-workflows",
    response_model=ListResponse[ProvisioningWorkflowRead],
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_provisioning_workflows(
    vendor: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_workflows.list_response(
        db, vendor, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-workflows",
    response_model=ProvisioningWorkflowRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_provisioning_workflow(
    payload: ProvisioningWorkflowCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_workflows.create(db, payload)


@router.get(
    "/provisioning-workflows/{workflow_id}",
    response_model=ProvisioningWorkflowRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_provisioning_workflow(workflow_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_workflows.get(db, workflow_id)


@router.patch(
    "/provisioning-workflows/{workflow_id}",
    response_model=ProvisioningWorkflowRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_provisioning_workflow(
    workflow_id: str, payload: ProvisioningWorkflowUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_workflows.update(db, workflow_id, payload)


@router.delete(
    "/provisioning-workflows/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_provisioning_workflow(workflow_id: str, db: Session = Depends(get_db)):
    provisioning_service.provisioning_workflows.delete(db, workflow_id)


@router.get(
    "/provisioning-steps",
    response_model=ListResponse[ProvisioningStepRead],
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_provisioning_steps(
    workflow_id: str | None = None,
    step_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="order_index"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_steps.list_response(
        db, workflow_id, step_type, is_active, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-steps",
    response_model=ProvisioningStepRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_provisioning_step(
    payload: ProvisioningStepCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_steps.create(db, payload)


@router.get(
    "/provisioning-steps/{step_id}",
    response_model=ProvisioningStepRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_provisioning_step(step_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_steps.get(db, step_id)


@router.patch(
    "/provisioning-steps/{step_id}",
    response_model=ProvisioningStepRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_provisioning_step(
    step_id: str, payload: ProvisioningStepUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_steps.update(db, step_id, payload)


@router.delete(
    "/provisioning-steps/{step_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_provisioning_step(step_id: str, db: Session = Depends(get_db)):
    provisioning_service.provisioning_steps.delete(db, step_id)


@router.get(
    "/provisioning-runs",
    response_model=ListResponse[ProvisioningRunRead],
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_provisioning_runs(
    workflow_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.provisioning_runs.list_response(
        db, workflow_id, status_filter, order_by, order_dir, limit, offset
    )


@router.post(
    "/provisioning-runs",
    response_model=ProvisioningRunRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_provisioning_run(
    payload: ProvisioningRunCreate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_runs.create(db, payload)


@router.post(
    "/provisioning-workflows/{workflow_id}/runs",
    response_model=ProvisioningRunRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def run_provisioning_workflow(
    workflow_id: str, payload: ProvisioningRunStart, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_runs.run(db, workflow_id, payload)


@router.get(
    "/provisioning-runs/{run_id}",
    response_model=ProvisioningRunRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_provisioning_run(run_id: str, db: Session = Depends(get_db)):
    return provisioning_service.provisioning_runs.get(db, run_id)


@router.patch(
    "/provisioning-runs/{run_id}",
    response_model=ProvisioningRunRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_provisioning_run(
    run_id: str, payload: ProvisioningRunUpdate, db: Session = Depends(get_db)
):
    return provisioning_service.provisioning_runs.update(db, run_id, payload)


@router.get(
    "/service-state-transitions",
    response_model=ListResponse[ServiceStateTransitionRead],
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_service_state_transitions(
    service_order_id: str | None = None,
    order_by: str = Query(default="changed_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return provisioning_service.service_state_transitions.list_response(
        db, service_order_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/service-state-transitions",
    response_model=ServiceStateTransitionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_service_state_transition(
    payload: ServiceStateTransitionCreate, db: Session = Depends(get_db)
):
    return provisioning_service.service_state_transitions.create(db, payload)


@router.get(
    "/service-state-transitions/{transition_id}",
    response_model=ServiceStateTransitionRead,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_service_state_transition(transition_id: str, db: Session = Depends(get_db)):
    return provisioning_service.service_state_transitions.get(db, transition_id)


@router.delete(
    "/service-state-transitions/{transition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["provisioning"],
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_service_state_transition(transition_id: str, db: Session = Depends(get_db)):
    provisioning_service.service_state_transitions.delete(db, transition_id)
