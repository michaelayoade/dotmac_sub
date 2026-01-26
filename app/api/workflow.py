from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.services.response import list_response
from app.db import SessionLocal
from app.schemas.common import ListResponse
from app.schemas.tickets import TicketRead
from app.schemas.workforce import WorkOrderRead
from app.schemas.projects import ProjectTaskRead
from app.schemas.workflow import (
    ProjectTaskStatusTransitionCreate,
    ProjectTaskStatusTransitionRead,
    ProjectTaskStatusTransitionUpdate,
    SlaBreachCreate,
    SlaBreachRead,
    SlaBreachUpdate,
    SlaClockCreate,
    SlaClockRead,
    SlaClockUpdate,
    SlaPolicyCreate,
    SlaPolicyRead,
    SlaPolicyUpdate,
    SlaTargetCreate,
    SlaTargetRead,
    SlaTargetUpdate,
    StatusTransitionRequest,
    TicketStatusTransitionCreate,
    TicketStatusTransitionRead,
    TicketStatusTransitionUpdate,
    WorkOrderStatusTransitionCreate,
    WorkOrderStatusTransitionRead,
    WorkOrderStatusTransitionUpdate,
)
from app.services import workflow as workflow_service

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/ticket-transitions",
    response_model=TicketStatusTransitionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["ticket-transitions"],
)
def create_ticket_transition(
    payload: TicketStatusTransitionCreate, db: Session = Depends(get_db)
):
    return workflow_service.ticket_transitions.create(db, payload)


@router.get(
    "/ticket-transitions/{transition_id}",
    response_model=TicketStatusTransitionRead,
    tags=["ticket-transitions"],
)
def get_ticket_transition(transition_id: str, db: Session = Depends(get_db)):
    return workflow_service.ticket_transitions.get(db, transition_id)


@router.get(
    "/ticket-transitions",
    response_model=ListResponse[TicketStatusTransitionRead],
    tags=["ticket-transitions"],
)
def list_ticket_transitions(
    from_status: str | None = None,
    to_status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = workflow_service.ticket_transitions.list(
        db, from_status, to_status, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/ticket-transitions/{transition_id}",
    response_model=TicketStatusTransitionRead,
    tags=["ticket-transitions"],
)
def update_ticket_transition(
    transition_id: str,
    payload: TicketStatusTransitionUpdate,
    db: Session = Depends(get_db),
):
    return workflow_service.ticket_transitions.update(db, transition_id, payload)


@router.delete(
    "/ticket-transitions/{transition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["ticket-transitions"],
)
def delete_ticket_transition(transition_id: str, db: Session = Depends(get_db)):
    workflow_service.ticket_transitions.delete(db, transition_id)


@router.post(
    "/work-order-transitions",
    response_model=WorkOrderStatusTransitionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["work-order-transitions"],
)
def create_work_order_transition(
    payload: WorkOrderStatusTransitionCreate, db: Session = Depends(get_db)
):
    return workflow_service.work_order_transitions.create(db, payload)


@router.get(
    "/work-order-transitions/{transition_id}",
    response_model=WorkOrderStatusTransitionRead,
    tags=["work-order-transitions"],
)
def get_work_order_transition(transition_id: str, db: Session = Depends(get_db)):
    return workflow_service.work_order_transitions.get(db, transition_id)


@router.get(
    "/work-order-transitions",
    response_model=ListResponse[WorkOrderStatusTransitionRead],
    tags=["work-order-transitions"],
)
def list_work_order_transitions(
    from_status: str | None = None,
    to_status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = workflow_service.work_order_transitions.list(
        db, from_status, to_status, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/work-order-transitions/{transition_id}",
    response_model=WorkOrderStatusTransitionRead,
    tags=["work-order-transitions"],
)
def update_work_order_transition(
    transition_id: str,
    payload: WorkOrderStatusTransitionUpdate,
    db: Session = Depends(get_db),
):
    return workflow_service.work_order_transitions.update(db, transition_id, payload)


@router.delete(
    "/work-order-transitions/{transition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["work-order-transitions"],
)
def delete_work_order_transition(transition_id: str, db: Session = Depends(get_db)):
    workflow_service.work_order_transitions.delete(db, transition_id)


@router.post(
    "/project-task-transitions",
    response_model=ProjectTaskStatusTransitionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["project-task-transitions"],
)
def create_project_task_transition(
    payload: ProjectTaskStatusTransitionCreate, db: Session = Depends(get_db)
):
    return workflow_service.project_task_transitions.create(db, payload)


@router.get(
    "/project-task-transitions/{transition_id}",
    response_model=ProjectTaskStatusTransitionRead,
    tags=["project-task-transitions"],
)
def get_project_task_transition(transition_id: str, db: Session = Depends(get_db)):
    return workflow_service.project_task_transitions.get(db, transition_id)


@router.get(
    "/project-task-transitions",
    response_model=ListResponse[ProjectTaskStatusTransitionRead],
    tags=["project-task-transitions"],
)
def list_project_task_transitions(
    from_status: str | None = None,
    to_status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = workflow_service.project_task_transitions.list(
        db, from_status, to_status, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/project-task-transitions/{transition_id}",
    response_model=ProjectTaskStatusTransitionRead,
    tags=["project-task-transitions"],
)
def update_project_task_transition(
    transition_id: str,
    payload: ProjectTaskStatusTransitionUpdate,
    db: Session = Depends(get_db),
):
    return workflow_service.project_task_transitions.update(
        db, transition_id, payload
    )


@router.delete(
    "/project-task-transitions/{transition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["project-task-transitions"],
)
def delete_project_task_transition(transition_id: str, db: Session = Depends(get_db)):
    workflow_service.project_task_transitions.delete(db, transition_id)


@router.post(
    "/sla-policies",
    response_model=SlaPolicyRead,
    status_code=status.HTTP_201_CREATED,
    tags=["sla-policies"],
)
def create_sla_policy(payload: SlaPolicyCreate, db: Session = Depends(get_db)):
    return workflow_service.sla_policies.create(db, payload)


@router.get("/sla-policies/{policy_id}", response_model=SlaPolicyRead, tags=["sla-policies"])
def get_sla_policy(policy_id: str, db: Session = Depends(get_db)):
    return workflow_service.sla_policies.get(db, policy_id)


@router.get(
    "/sla-policies",
    response_model=ListResponse[SlaPolicyRead],
    tags=["sla-policies"],
)
def list_sla_policies(
    entity_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = workflow_service.sla_policies.list(
        db, entity_type, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/sla-policies/{policy_id}",
    response_model=SlaPolicyRead,
    tags=["sla-policies"],
)
def update_sla_policy(
    policy_id: str, payload: SlaPolicyUpdate, db: Session = Depends(get_db)
):
    return workflow_service.sla_policies.update(db, policy_id, payload)


@router.delete(
    "/sla-policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sla-policies"],
)
def delete_sla_policy(policy_id: str, db: Session = Depends(get_db)):
    workflow_service.sla_policies.delete(db, policy_id)


@router.post(
    "/sla-targets",
    response_model=SlaTargetRead,
    status_code=status.HTTP_201_CREATED,
    tags=["sla-targets"],
)
def create_sla_target(payload: SlaTargetCreate, db: Session = Depends(get_db)):
    return workflow_service.sla_targets.create(db, payload)


@router.get(
    "/sla-targets/{target_id}", response_model=SlaTargetRead, tags=["sla-targets"]
)
def get_sla_target(target_id: str, db: Session = Depends(get_db)):
    return workflow_service.sla_targets.get(db, target_id)


@router.get(
    "/sla-targets",
    response_model=ListResponse[SlaTargetRead],
    tags=["sla-targets"],
)
def list_sla_targets(
    policy_id: str | None = None,
    priority: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = workflow_service.sla_targets.list(
        db, policy_id, priority, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/sla-targets/{target_id}",
    response_model=SlaTargetRead,
    tags=["sla-targets"],
)
def update_sla_target(
    target_id: str, payload: SlaTargetUpdate, db: Session = Depends(get_db)
):
    return workflow_service.sla_targets.update(db, target_id, payload)


@router.delete(
    "/sla-targets/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sla-targets"],
)
def delete_sla_target(target_id: str, db: Session = Depends(get_db)):
    workflow_service.sla_targets.delete(db, target_id)


@router.post(
    "/sla-clocks",
    response_model=SlaClockRead,
    status_code=status.HTTP_201_CREATED,
    tags=["sla-clocks"],
)
def create_sla_clock(payload: SlaClockCreate, db: Session = Depends(get_db)):
    return workflow_service.sla_clocks.create(db, payload)


@router.get("/sla-clocks/{clock_id}", response_model=SlaClockRead, tags=["sla-clocks"])
def get_sla_clock(clock_id: str, db: Session = Depends(get_db)):
    return workflow_service.sla_clocks.get(db, clock_id)


@router.get(
    "/sla-clocks",
    response_model=ListResponse[SlaClockRead],
    tags=["sla-clocks"],
)
def list_sla_clocks(
    policy_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = workflow_service.sla_clocks.list(
        db, policy_id, entity_type, entity_id, status, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/sla-clocks/{clock_id}",
    response_model=SlaClockRead,
    tags=["sla-clocks"],
)
def update_sla_clock(
    clock_id: str, payload: SlaClockUpdate, db: Session = Depends(get_db)
):
    return workflow_service.sla_clocks.update(db, clock_id, payload)


@router.delete(
    "/sla-clocks/{clock_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sla-clocks"],
)
def delete_sla_clock(clock_id: str, db: Session = Depends(get_db)):
    workflow_service.sla_clocks.delete(db, clock_id)


@router.post(
    "/sla-breaches",
    response_model=SlaBreachRead,
    status_code=status.HTTP_201_CREATED,
    tags=["sla-breaches"],
)
def create_sla_breach(payload: SlaBreachCreate, db: Session = Depends(get_db)):
    return workflow_service.sla_breaches.create(db, payload)


@router.get(
    "/sla-breaches/{breach_id}", response_model=SlaBreachRead, tags=["sla-breaches"]
)
def get_sla_breach(breach_id: str, db: Session = Depends(get_db)):
    return workflow_service.sla_breaches.get(db, breach_id)


@router.get(
    "/sla-breaches",
    response_model=ListResponse[SlaBreachRead],
    tags=["sla-breaches"],
)
def list_sla_breaches(
    clock_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = workflow_service.sla_breaches.list(
        db, clock_id, status, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/sla-breaches/{breach_id}",
    response_model=SlaBreachRead,
    tags=["sla-breaches"],
)
def update_sla_breach(
    breach_id: str, payload: SlaBreachUpdate, db: Session = Depends(get_db)
):
    return workflow_service.sla_breaches.update(db, breach_id, payload)


@router.delete(
    "/sla-breaches/{breach_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sla-breaches"],
)
def delete_sla_breach(breach_id: str, db: Session = Depends(get_db)):
    workflow_service.sla_breaches.delete(db, breach_id)


@router.post(
    "/tickets/{ticket_id}/transition",
    response_model=TicketRead,
    tags=["tickets"],
)
def transition_ticket(
    ticket_id: str, payload: StatusTransitionRequest, db: Session = Depends(get_db)
):
    return workflow_service.transition_ticket(db, ticket_id, payload)


@router.post(
    "/work-orders/{work_order_id}/transition",
    response_model=WorkOrderRead,
    tags=["work-orders"],
)
def transition_work_order(
    work_order_id: str, payload: StatusTransitionRequest, db: Session = Depends(get_db)
):
    return workflow_service.transition_work_order(db, work_order_id, payload)


@router.post(
    "/project-tasks/{task_id}/transition",
    response_model=ProjectTaskRead,
    tags=["project-tasks"],
)
def transition_project_task(
    task_id: str, payload: StatusTransitionRequest, db: Session = Depends(get_db)
):
    return workflow_service.transition_project_task(db, task_id, payload)
