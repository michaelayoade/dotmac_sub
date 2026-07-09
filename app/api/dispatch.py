from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.dispatch import (
    AvailabilityBlockCreate,
    AvailabilityBlockRead,
    AvailabilityBlockUpdate,
    DispatchRuleCreate,
    DispatchRuleRead,
    DispatchRuleUpdate,
    ShiftCreate,
    ShiftRead,
    ShiftUpdate,
    SkillCreate,
    SkillRead,
    SkillUpdate,
    TechnicianProfileCreate,
    TechnicianProfileRead,
    TechnicianProfileUpdate,
    TechnicianSkillCreate,
    TechnicianSkillRead,
    TechnicianSkillUpdate,
    WorkOrderAssignmentQueueCreate,
    WorkOrderAssignmentQueueRead,
    WorkOrderAssignmentQueueUpdate,
)
from app.services import dispatch as dispatch_service

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


@router.post("/skills", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_skill(payload: SkillCreate, db: Session = Depends(get_db)):
    return dispatch_service.skills.create(db, payload)


@router.get("/skills", response_model=ListResponse[SkillRead])
def list_skills(
    is_active: bool | None = True,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.skills.list_response(
        db,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get("/skills/{skill_id}", response_model=SkillRead)
def get_skill(skill_id: str, db: Session = Depends(get_db)):
    return dispatch_service.skills.get(db, skill_id)


@router.patch("/skills/{skill_id}", response_model=SkillRead)
def update_skill(skill_id: str, payload: SkillUpdate, db: Session = Depends(get_db)):
    return dispatch_service.skills.update(db, skill_id, payload)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: str, db: Session = Depends(get_db)):
    dispatch_service.skills.delete(db, skill_id)


@router.post(
    "/technicians",
    response_model=TechnicianProfileRead,
    status_code=status.HTTP_201_CREATED,
)
def create_technician(payload: TechnicianProfileCreate, db: Session = Depends(get_db)):
    return dispatch_service.technician_profiles.create(db, payload)


@router.get("/technicians", response_model=ListResponse[TechnicianProfileRead])
def list_technicians(
    region: str | None = None,
    is_active: bool | None = True,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.technician_profiles.list_response(
        db,
        region=region,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get("/technicians/{technician_id}", response_model=TechnicianProfileRead)
def get_technician(technician_id: str, db: Session = Depends(get_db)):
    return dispatch_service.technician_profiles.get(db, technician_id)


@router.patch("/technicians/{technician_id}", response_model=TechnicianProfileRead)
def update_technician(
    technician_id: str,
    payload: TechnicianProfileUpdate,
    db: Session = Depends(get_db),
):
    return dispatch_service.technician_profiles.update(db, technician_id, payload)


@router.delete("/technicians/{technician_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_technician(technician_id: str, db: Session = Depends(get_db)):
    dispatch_service.technician_profiles.delete(db, technician_id)


@router.post(
    "/technician-skills",
    response_model=TechnicianSkillRead,
    status_code=status.HTTP_201_CREATED,
)
def create_technician_skill(
    payload: TechnicianSkillCreate, db: Session = Depends(get_db)
):
    return dispatch_service.technician_skills.create(db, payload)


@router.get("/technician-skills", response_model=ListResponse[TechnicianSkillRead])
def list_technician_skills(
    technician_id: str | None = None,
    skill_id: str | None = None,
    is_active: bool | None = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.technician_skills.list_response(
        db,
        technician_id=technician_id,
        skill_id=skill_id,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.patch("/technician-skills/{row_id}", response_model=TechnicianSkillRead)
def update_technician_skill(
    row_id: str,
    payload: TechnicianSkillUpdate,
    db: Session = Depends(get_db),
):
    return dispatch_service.technician_skills.update(db, row_id, payload)


@router.post("/shifts", response_model=ShiftRead, status_code=status.HTTP_201_CREATED)
def create_shift(payload: ShiftCreate, db: Session = Depends(get_db)):
    return dispatch_service.shifts.create(db, payload)


@router.get("/shifts", response_model=ListResponse[ShiftRead])
def list_shifts(
    technician_id: str | None = None,
    is_active: bool | None = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.shifts.list_response(
        db,
        technician_id=technician_id,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.patch("/shifts/{shift_id}", response_model=ShiftRead)
def update_shift(shift_id: str, payload: ShiftUpdate, db: Session = Depends(get_db)):
    return dispatch_service.shifts.update(db, shift_id, payload)


@router.post(
    "/availability-blocks",
    response_model=AvailabilityBlockRead,
    status_code=status.HTTP_201_CREATED,
)
def create_availability_block(
    payload: AvailabilityBlockCreate, db: Session = Depends(get_db)
):
    return dispatch_service.availability_blocks.create(db, payload)


@router.get("/availability-blocks", response_model=ListResponse[AvailabilityBlockRead])
def list_availability_blocks(
    technician_id: str | None = None,
    is_active: bool | None = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.availability_blocks.list_response(
        db,
        technician_id=technician_id,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/availability-blocks/{block_id}",
    response_model=AvailabilityBlockRead,
)
def update_availability_block(
    block_id: str,
    payload: AvailabilityBlockUpdate,
    db: Session = Depends(get_db),
):
    return dispatch_service.availability_blocks.update(db, block_id, payload)


@router.post(
    "/rules",
    response_model=DispatchRuleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_dispatch_rule(payload: DispatchRuleCreate, db: Session = Depends(get_db)):
    return dispatch_service.dispatch_rules.create(db, payload)


@router.get("/rules", response_model=ListResponse[DispatchRuleRead])
def list_dispatch_rules(
    work_type: str | None = None,
    region: str | None = None,
    is_active: bool | None = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.dispatch_rules.list_response(
        db,
        work_type=work_type,
        region=region,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.patch("/rules/{rule_id}", response_model=DispatchRuleRead)
def update_dispatch_rule(
    rule_id: str, payload: DispatchRuleUpdate, db: Session = Depends(get_db)
):
    return dispatch_service.dispatch_rules.update(db, rule_id, payload)


@router.post(
    "/assignment-queue",
    response_model=WorkOrderAssignmentQueueRead,
    status_code=status.HTTP_201_CREATED,
)
def create_assignment_queue_item(
    payload: WorkOrderAssignmentQueueCreate, db: Session = Depends(get_db)
):
    return dispatch_service.assignment_queue.create(db, payload)


@router.get(
    "/assignment-queue",
    response_model=ListResponse[WorkOrderAssignmentQueueRead],
)
def list_assignment_queue(
    status: str | None = None,
    crm_work_order_id: str | None = None,
    assigned_technician_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.assignment_queue.list_response(
        db,
        status=status,
        crm_work_order_id=crm_work_order_id,
        assigned_technician_id=assigned_technician_id,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/assignment-queue/{queue_id}",
    response_model=WorkOrderAssignmentQueueRead,
)
def update_assignment_queue_item(
    queue_id: str,
    payload: WorkOrderAssignmentQueueUpdate,
    db: Session = Depends(get_db),
):
    return dispatch_service.assignment_queue.update(db, queue_id, payload)
