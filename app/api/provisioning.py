"""
Provisioning REST API Endpoints

Provides REST API for:
- Service Orders CRUD and workflow execution
- Install Appointments CRUD
- Provisioning Tasks CRUD
- Service State Transitions CRUD
- Provisioning Workflows CRUD
- Provisioning Steps CRUD
- Provisioning Runs listing and execution
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.db import get_db
from app.models.provisioning import (
    AppointmentStatus,
    ProvisioningRunStatus,
    ProvisioningStepType,
    ProvisioningVendor,
    ServiceOrderStatus,
    TaskStatus,
)
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    InstallAppointmentRead,
    InstallAppointmentUpdate,
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
    ServiceStateTransitionUpdate,
)
from app.services.provisioning import (
    InstallAppointments,
    ProvisioningRuns,
    ProvisioningSteps,
    ProvisioningTasks,
    ProvisioningWorkflows,
    ServiceOrders,
    ServiceStateTransitions,
)

router = APIRouter(prefix="/provisioning", tags=["provisioning"])


# =============================================================================
# SERVICE ORDER ENDPOINTS
# =============================================================================

@router.get(
    "/orders/stats",
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_order_stats(db: Session = Depends(get_db)):
    """Get aggregated service order statistics."""
    return ServiceOrders.get_dashboard_stats(db)


@router.get(
    "/orders",
    response_model=dict,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_orders(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("created_at"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    subscriber_id: UUID | None = None,
    subscription_id: UUID | None = None,
    status: ServiceOrderStatus | None = None,
):
    """List service orders with filtering and pagination."""
    return ServiceOrders.list_response(
        db,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id,
        status=status,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/orders",
    response_model=ServiceOrderRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_order(
    payload: ServiceOrderCreate,
    db: Session = Depends(get_db),
):
    """Create a new service order."""
    return ServiceOrders.create(db, payload)


@router.get(
    "/orders/{order_id}",
    response_model=ServiceOrderRead,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_order(
    order_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a service order by ID."""
    return ServiceOrders.get(db, str(order_id))


@router.patch(
    "/orders/{order_id}",
    response_model=ServiceOrderRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_order(
    order_id: UUID,
    payload: ServiceOrderUpdate,
    db: Session = Depends(get_db),
):
    """Update a service order."""
    return ServiceOrders.update(db, str(order_id), payload)


@router.delete(
    "/orders/{order_id}",
    status_code=204,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_order(
    order_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete a service order."""
    ServiceOrders.delete(db, str(order_id))


@router.post(
    "/orders/{order_id}/run-workflow",
    response_model=ProvisioningRunRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def run_workflow_for_order(
    order_id: UUID,
    workflow_id: UUID = Query(...),
    db: Session = Depends(get_db),
):
    """Run a provisioning workflow for a service order."""
    return ServiceOrders.run_for_order(db, str(order_id), str(workflow_id))


# =============================================================================
# INSTALL APPOINTMENT ENDPOINTS
# =============================================================================

@router.get(
    "/appointments",
    response_model=dict,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_appointments(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("scheduled_start"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    service_order_id: UUID | None = None,
    status: AppointmentStatus | None = None,
):
    """List install appointments with filtering and pagination."""
    return InstallAppointments.list_response(
        db,
        service_order_id=service_order_id,
        status=status,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/appointments",
    response_model=InstallAppointmentRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_appointment(
    payload: InstallAppointmentCreate,
    db: Session = Depends(get_db),
):
    """Create a new install appointment."""
    return InstallAppointments.create(db, payload)


@router.get(
    "/appointments/{appointment_id}",
    response_model=InstallAppointmentRead,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_appointment(
    appointment_id: UUID,
    db: Session = Depends(get_db),
):
    """Get an install appointment by ID."""
    return InstallAppointments.get(db, str(appointment_id))


@router.patch(
    "/appointments/{appointment_id}",
    response_model=InstallAppointmentRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_appointment(
    appointment_id: UUID,
    payload: InstallAppointmentUpdate,
    db: Session = Depends(get_db),
):
    """Update an install appointment."""
    return InstallAppointments.update(db, str(appointment_id), payload)


@router.delete(
    "/appointments/{appointment_id}",
    status_code=204,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_appointment(
    appointment_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete an install appointment."""
    InstallAppointments.delete(db, str(appointment_id))


# =============================================================================
# PROVISIONING TASK ENDPOINTS
# =============================================================================

@router.get(
    "/tasks",
    response_model=dict,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_tasks(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("created_at"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    service_order_id: UUID | None = None,
    status: TaskStatus | None = None,
):
    """List provisioning tasks with filtering and pagination."""
    return ProvisioningTasks.list_response(
        db,
        service_order_id=service_order_id,
        status=status,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/tasks",
    response_model=ProvisioningTaskRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_task(
    payload: ProvisioningTaskCreate,
    db: Session = Depends(get_db),
):
    """Create a new provisioning task."""
    return ProvisioningTasks.create(db, payload)


@router.get(
    "/tasks/{task_id}",
    response_model=ProvisioningTaskRead,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_task(
    task_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a provisioning task by ID."""
    return ProvisioningTasks.get(db, str(task_id))


@router.patch(
    "/tasks/{task_id}",
    response_model=ProvisioningTaskRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_task(
    task_id: UUID,
    payload: ProvisioningTaskUpdate,
    db: Session = Depends(get_db),
):
    """Update a provisioning task."""
    return ProvisioningTasks.update(db, str(task_id), payload)


@router.delete(
    "/tasks/{task_id}",
    status_code=204,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_task(
    task_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete a provisioning task."""
    ProvisioningTasks.delete(db, str(task_id))


# =============================================================================
# SERVICE STATE TRANSITION ENDPOINTS
# =============================================================================

@router.get(
    "/transitions",
    response_model=dict,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_transitions(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("changed_at"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    service_order_id: UUID | None = None,
):
    """List service state transitions with filtering and pagination."""
    return ServiceStateTransitions.list_response(
        db,
        service_order_id=service_order_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/transitions",
    response_model=ServiceStateTransitionRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_transition(
    payload: ServiceStateTransitionCreate,
    db: Session = Depends(get_db),
):
    """Create a new service state transition."""
    return ServiceStateTransitions.create(db, payload)


@router.get(
    "/transitions/{transition_id}",
    response_model=ServiceStateTransitionRead,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_transition(
    transition_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a service state transition by ID."""
    return ServiceStateTransitions.get(db, str(transition_id))


@router.patch(
    "/transitions/{transition_id}",
    response_model=ServiceStateTransitionRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_transition(
    transition_id: UUID,
    payload: ServiceStateTransitionUpdate,
    db: Session = Depends(get_db),
):
    """Update a service state transition."""
    return ServiceStateTransitions.update(db, str(transition_id), payload)


# =============================================================================
# PROVISIONING WORKFLOW ENDPOINTS
# =============================================================================

@router.get(
    "/workflows",
    response_model=dict,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_workflows(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("created_at"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    vendor: ProvisioningVendor | None = None,
    is_active: bool | None = None,
):
    """List provisioning workflows with filtering and pagination."""
    return ProvisioningWorkflows.list_response(
        db,
        vendor=vendor,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/workflows",
    response_model=ProvisioningWorkflowRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_workflow(
    payload: ProvisioningWorkflowCreate,
    db: Session = Depends(get_db),
):
    """Create a new provisioning workflow."""
    return ProvisioningWorkflows.create(db, payload)


@router.get(
    "/workflows/{workflow_id}",
    response_model=ProvisioningWorkflowRead,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_workflow(
    workflow_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a provisioning workflow by ID."""
    return ProvisioningWorkflows.get(db, str(workflow_id))


@router.patch(
    "/workflows/{workflow_id}",
    response_model=ProvisioningWorkflowRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_workflow(
    workflow_id: UUID,
    payload: ProvisioningWorkflowUpdate,
    db: Session = Depends(get_db),
):
    """Update a provisioning workflow."""
    return ProvisioningWorkflows.update(db, str(workflow_id), payload)


@router.delete(
    "/workflows/{workflow_id}",
    status_code=204,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_workflow(
    workflow_id: UUID,
    db: Session = Depends(get_db),
):
    """Soft-delete a provisioning workflow (sets is_active=False)."""
    ProvisioningWorkflows.delete(db, str(workflow_id))


@router.post(
    "/workflows/{workflow_id}/run",
    response_model=ProvisioningRunRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def run_workflow(
    workflow_id: UUID,
    payload: ProvisioningRunStart | None = None,
    db: Session = Depends(get_db),
):
    """Execute a provisioning workflow, creating a new run."""
    return ProvisioningRuns.run(db, str(workflow_id), payload)


# =============================================================================
# PROVISIONING STEP ENDPOINTS
# =============================================================================

@router.get(
    "/steps",
    response_model=dict,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_steps(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("order_index"),
    order_dir: str = Query("asc", pattern="^(asc|desc)$"),
    workflow_id: UUID | None = None,
    step_type: ProvisioningStepType | None = None,
    is_active: bool | None = None,
):
    """List provisioning steps with filtering and pagination."""
    return ProvisioningSteps.list_response(
        db,
        workflow_id=workflow_id,
        step_type=step_type,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/steps",
    response_model=ProvisioningStepRead,
    status_code=201,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def create_step(
    payload: ProvisioningStepCreate,
    db: Session = Depends(get_db),
):
    """Create a new provisioning step."""
    return ProvisioningSteps.create(db, payload)


@router.get(
    "/steps/{step_id}",
    response_model=ProvisioningStepRead,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_step(
    step_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a provisioning step by ID."""
    return ProvisioningSteps.get(db, str(step_id))


@router.patch(
    "/steps/{step_id}",
    response_model=ProvisioningStepRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_step(
    step_id: UUID,
    payload: ProvisioningStepUpdate,
    db: Session = Depends(get_db),
):
    """Update a provisioning step."""
    return ProvisioningSteps.update(db, str(step_id), payload)


@router.delete(
    "/steps/{step_id}",
    status_code=204,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def delete_step(
    step_id: UUID,
    db: Session = Depends(get_db),
):
    """Soft-delete a provisioning step (sets is_active=False)."""
    ProvisioningSteps.delete(db, str(step_id))


# =============================================================================
# PROVISIONING RUN ENDPOINTS
# =============================================================================

@router.get(
    "/runs",
    response_model=dict,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_runs(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_by: str = Query("created_at"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    workflow_id: UUID | None = None,
    status: ProvisioningRunStatus | None = None,
):
    """List provisioning runs with filtering and pagination."""
    return ProvisioningRuns.list_response(
        db,
        workflow_id=workflow_id,
        status=status,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/runs/{run_id}",
    response_model=ProvisioningRunRead,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def get_run(
    run_id: UUID,
    db: Session = Depends(get_db),
):
    """Get a provisioning run by ID."""
    return ProvisioningRuns.get(db, str(run_id))


@router.patch(
    "/runs/{run_id}",
    response_model=ProvisioningRunRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_run(
    run_id: UUID,
    payload: ProvisioningRunUpdate,
    db: Session = Depends(get_db),
):
    """Update a provisioning run."""
    return ProvisioningRuns.update(db, str(run_id), payload)
