from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
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
    FiberFieldVerificationJobPlanExecuteRead,
    FiberFieldVerificationJobPlanExecuteRequest,
    FiberFieldVerificationJobPlanPreviewRead,
    FiberFieldVerificationJobPlanRequest,
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
    WorkOrderAssignmentOutcomeRead,
    WorkOrderAssignmentPreviewRead,
    WorkOrderAssignmentPreviewRequest,
    WorkOrderAssignmentQueueCreate,
    WorkOrderAssignmentQueueRead,
    WorkOrderAssignmentQueueUpdate,
    WorkOrderAssignmentRequest,
    WorkOrderClassificationRead,
    WorkOrderClassificationRequest,
    WorkOrderHeaderCreate,
    WorkOrderHeaderRead,
    WorkOrderHeaderUpdate,
)
from app.services import dispatch as dispatch_service
from app.services.auth_dependencies import (
    require_permission,
    require_permission_admission,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.network.fiber_field_verification_job_plan_contracts import (
    FiberJobPlanCommand,
    FiberJobPlanQuery,
    FiberPlanAuditPrincipal,
)
from app.services.network.fiber_field_verification_job_plans import (
    FiberFieldVerificationJobPlanError,
    execute_fiber_field_verification_job_plan,
    preview_fiber_field_verification_job_plan,
    serialize_fiber_plan_command,
)
from app.services.network.fiber_field_verification_job_scope import (
    planned_scope_payload,
)
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.work_order_assignment_contracts import (
    AssignmentOperation,
    AssignmentTarget,
    TechnicianAssignmentTarget,
    VendorAssignmentTarget,
    WorkOrderAssignmentCommand,
    WorkOrderAssignmentQuery,
)
from app.services.work_order_classification_contracts import ClassifyWorkOrderCommand
from app.services.work_order_errors import WorkOrderCommandError
from app.services.work_order_templates import WorkOrderTemplateError

router = APIRouter(prefix="/dispatch", tags=["dispatch"])

# Granular gates. The router is mounted with an operations:dispatch:read floor
# (main.py), so read endpoints inherit it; mutations declare their own permission.
_DISPATCH_WRITE = Depends(require_permission("operations:dispatch:write"))


def _assignment_context(
    access: PermissionAdmission,
    *,
    request_id: str | None,
    command_id: UUID | None = None,
) -> CommandContext:
    identity = command_id or (
        uuid5(NAMESPACE_URL, f"work-order-assignment:{request_id}")
        if request_id
        else uuid4()
    )
    return CommandContext(
        command_id=identity,
        correlation_id=uuid5(NAMESPACE_URL, request_id) if request_id else identity,
        actor=str(access.principal_id),
        scope="operations:dispatch:assign",
        reason="Dispatch work-order assignment",
        idempotency_key=str(identity),
    )


def _assignment_target(payload: WorkOrderAssignmentPreviewRequest) -> AssignmentTarget:
    if payload.vendor_id is not None:
        return VendorAssignmentTarget(payload.vendor_id, payload.vendor_user_id)
    assert payload.technician_id is not None
    return TechnicianAssignmentTarget(payload.technician_id)


@router.post(
    "/skills",
    response_model=SkillRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_DISPATCH_WRITE],
)
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


@router.patch(
    "/skills/{skill_id}", response_model=SkillRead, dependencies=[_DISPATCH_WRITE]
)
def update_skill(skill_id: str, payload: SkillUpdate, db: Session = Depends(get_db)):
    return dispatch_service.skills.update(db, skill_id, payload)


@router.delete(
    "/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_DISPATCH_WRITE],
)
def delete_skill(skill_id: str, db: Session = Depends(get_db)):
    dispatch_service.skills.delete(db, skill_id)


@router.post(
    "/technicians",
    response_model=TechnicianProfileRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_DISPATCH_WRITE],
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


@router.patch(
    "/technicians/{technician_id}",
    response_model=TechnicianProfileRead,
    dependencies=[_DISPATCH_WRITE],
)
def update_technician(
    technician_id: str,
    payload: TechnicianProfileUpdate,
    db: Session = Depends(get_db),
):
    return dispatch_service.technician_profiles.update(db, technician_id, payload)


@router.delete(
    "/technicians/{technician_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_DISPATCH_WRITE],
)
def delete_technician(technician_id: str, db: Session = Depends(get_db)):
    dispatch_service.technician_profiles.delete(db, technician_id)


@router.post(
    "/technician-skills",
    response_model=TechnicianSkillRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_DISPATCH_WRITE],
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


@router.patch(
    "/technician-skills/{row_id}",
    response_model=TechnicianSkillRead,
    dependencies=[_DISPATCH_WRITE],
)
def update_technician_skill(
    row_id: str,
    payload: TechnicianSkillUpdate,
    db: Session = Depends(get_db),
):
    return dispatch_service.technician_skills.update(db, row_id, payload)


@router.post(
    "/shifts",
    response_model=ShiftRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_DISPATCH_WRITE],
)
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


@router.patch(
    "/shifts/{shift_id}", response_model=ShiftRead, dependencies=[_DISPATCH_WRITE]
)
def update_shift(shift_id: str, payload: ShiftUpdate, db: Session = Depends(get_db)):
    return dispatch_service.shifts.update(db, shift_id, payload)


@router.post(
    "/availability-blocks",
    response_model=AvailabilityBlockRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_DISPATCH_WRITE],
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
    dependencies=[_DISPATCH_WRITE],
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
    dependencies=[_DISPATCH_WRITE],
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


@router.patch(
    "/rules/{rule_id}", response_model=DispatchRuleRead, dependencies=[_DISPATCH_WRITE]
)
def update_dispatch_rule(
    rule_id: str, payload: DispatchRuleUpdate, db: Session = Depends(get_db)
):
    return dispatch_service.dispatch_rules.update(db, rule_id, payload)


def _job_plan_query(payload: FiberFieldVerificationJobPlanRequest) -> FiberJobPlanQuery:
    target: AssignmentTarget | None = None
    if payload.assigned_vendor_id is not None:
        target = VendorAssignmentTarget(
            payload.assigned_vendor_id, payload.assigned_vendor_user_id
        )
    elif payload.assigned_technician_id is not None:
        target = TechnicianAssignmentTarget(payload.assigned_technician_id)
    return FiberJobPlanQuery(
        expected_worklist_report_sha256=payload.expected_worklist_report_sha256,
        staged_feature_ids=tuple(payload.staged_feature_ids),
        subscriber_id=payload.subscriber_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        address=payload.address,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        target=target,
        assignment_reason=payload.assignment_reason,
        idempotency_key=payload.idempotency_key,
    )


def _fiber_plan_error(exc: FiberFieldVerificationJobPlanError) -> HTTPException:
    kind = exc.code.rsplit(".", 1)[-1]
    status_code = {
        "forbidden": 403,
        "not_found": 404,
        "stale_worklist": 409,
        "stale_plan": 409,
    }.get(kind, 422)
    return HTTPException(status_code=status_code, detail=exc.message)


@router.post(
    "/field-verification-job-plans/preview",
    response_model=FiberFieldVerificationJobPlanPreviewRead,
)
def preview_field_verification_job_plan(
    payload: FiberFieldVerificationJobPlanRequest,
    auth: dict = Depends(require_permission("operations:dispatch:assign")),
    write_auth: dict = Depends(require_permission("operations:dispatch:write")),
    network_auth: dict = Depends(require_permission("network:fiber:read")),
    db: Session = Depends(get_db),
):
    del auth, write_auth, network_auth
    try:
        plan = preview_fiber_field_verification_job_plan(
            db, query=_job_plan_query(payload)
        )
        return {
            "command": serialize_fiber_plan_command(plan),
            **planned_scope_payload(plan.scope),
            "plan_sha256": plan.plan_sha256,
        }
    except FiberFieldVerificationJobPlanError as exc:
        raise _fiber_plan_error(exc) from exc


@router.post(
    "/field-verification-job-plans/execute",
    response_model=FiberFieldVerificationJobPlanExecuteRead,
    status_code=status.HTTP_201_CREATED,
)
def execute_field_verification_job_plan(
    payload: FiberFieldVerificationJobPlanExecuteRequest,
    access: PermissionAdmission = Depends(
        require_permission_admission("operations:dispatch:assign")
    ),
    write_access: PermissionAdmission = Depends(
        require_permission_admission("operations:dispatch:write")
    ),
    network_access: PermissionAdmission = Depends(
        require_permission_admission("network:fiber:read")
    ),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    if any(
        proof.principal_id != access.principal_id
        or proof.principal_type != access.principal_type
        for proof in (write_access, network_access)
    ):
        raise HTTPException(
            status_code=403,
            detail="Permission admissions must identify the same principal",
        )
    admitted = PermissionAdmission(
        principal_id=access.principal_id,
        principal_type=access.principal_type,
        admitted_permissions=access.admitted_permissions
        | write_access.admitted_permissions
        | network_access.admitted_permissions,
    )
    query = _job_plan_query(payload)
    command_id = uuid5(NAMESPACE_URL, f"fiber-job-plan:{query.idempotency_key}")
    context = CommandContext(
        command_id=command_id,
        correlation_id=uuid5(NAMESPACE_URL, request_id) if request_id else command_id,
        actor=str(admitted.principal_id),
        scope="operations:dispatch:assign",
        reason=query.assignment_reason or "Create fiber field-verification work order",
        idempotency_key=query.idempotency_key,
    )
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = execute_fiber_field_verification_job_plan(
            db,
            command=FiberJobPlanCommand(
                query=query,
                expected_plan_sha256=payload.expected_plan_sha256,
                context=context,
                access=admitted,
                audit=FiberPlanAuditPrincipal(admitted.principal_id, request_id),
            ),
        )
        return {
            "work_order": outcome.work_order,
            "assignment": outcome.assignment,
            "replayed": outcome.replayed,
            "plan": {
                "command": serialize_fiber_plan_command(outcome.plan),
                **planned_scope_payload(outcome.plan.scope),
                "plan_sha256": outcome.plan.plan_sha256,
            },
        }
    except FiberFieldVerificationJobPlanError as exc:
        raise _fiber_plan_error(exc) from exc


@router.post(
    "/work-orders",
    response_model=WorkOrderHeaderRead,
    status_code=status.HTTP_201_CREATED,
)
def create_work_order_header(
    payload: WorkOrderHeaderCreate,
    auth: dict = Depends(require_permission("operations:dispatch:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    return dispatch_service.work_order_headers.create(
        db,
        payload,
        auth=auth,
        request_id=request_id,
        idempotency_key=idempotency_key,
    )


@router.get("/work-orders", response_model=ListResponse[WorkOrderHeaderRead])
def list_work_order_headers(
    status: str | None = None,
    subscriber_id: str | None = None,
    work_type: str | None = None,
    is_active: bool | None = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.work_order_headers.list_response(
        db,
        status=status,
        subscriber_id=subscriber_id,
        work_type=work_type,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.get("/work-orders/{work_order_id}", response_model=WorkOrderHeaderRead)
def get_work_order_header(work_order_id: str, db: Session = Depends(get_db)):
    return dispatch_service.work_order_headers.get(db, work_order_id)


@router.patch(
    "/work-orders/{work_order_id}",
    response_model=WorkOrderHeaderRead,
)
def update_work_order_header(
    work_order_id: str,
    payload: WorkOrderHeaderUpdate,
    auth: dict = Depends(require_permission("operations:dispatch:write")),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    return dispatch_service.work_order_headers.update(
        db,
        work_order_id,
        payload,
        auth=auth,
        request_id=request_id,
    )


@router.post(
    "/work-orders/{work_order_id}/assignment-preview",
    response_model=WorkOrderAssignmentPreviewRead,
)
def preview_work_order_assignment(
    work_order_id: str,
    payload: WorkOrderAssignmentPreviewRequest,
    auth: dict = Depends(require_permission("operations:dispatch:assign")),
    db: Session = Depends(get_db),
):
    del auth  # Authorization is enforced by the dependency; previews do not write.
    return dispatch_service.work_order_commands.preview_assignment(
        db,
        query=WorkOrderAssignmentQuery(
            work_order_public_id=work_order_id,
            target=_assignment_target(payload),
            scheduled_start=payload.scheduled_start,
            scheduled_end=payload.scheduled_end,
            status=payload.status,
            expected_revision=payload.expected_revision,
            expected_assignment_generation=payload.expected_assignment_generation,
        ),
    )


@router.post(
    "/work-orders/{work_order_id}/assignment",
    response_model=WorkOrderAssignmentOutcomeRead,
)
def assign_work_order(
    work_order_id: str,
    payload: WorkOrderAssignmentRequest,
    access: PermissionAdmission = Depends(
        require_permission_admission("operations:dispatch:assign")
    ),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    command = WorkOrderAssignmentCommand(
        access=access,
        operation=AssignmentOperation(payload.operation),
        work_order_public_id=work_order_id,
        target=_assignment_target(payload),
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        status=payload.status,
        reason=payload.reason,
        dispatch_rule_id=payload.dispatch_rule_id,
        expected_revision=payload.expected_revision,
        expected_assignment_generation=payload.expected_assignment_generation,
    )
    context = _assignment_context(
        access, request_id=request_id, command_id=payload.command_id
    )
    db_session_adapter.release_read_transaction(db)
    outcome = dispatch_service.work_order_commands.assign(
        db, command=command, context=context
    )
    return WorkOrderAssignmentOutcomeRead(
        vendor_user_id=outcome.target.vendor_user_id
        if isinstance(outcome.target, VendorAssignmentTarget)
        else None,
        assignment_generation=outcome.assignment_generation,
        queue_id=outcome.queue_id,
        work_order_id=outcome.work_order_id,
        technician_id=outcome.target.technician_id
        if isinstance(outcome.target, TechnicianAssignmentTarget)
        else None,
        vendor_id=outcome.target.vendor_id
        if isinstance(outcome.target, VendorAssignmentTarget)
        else None,
        status=outcome.status,
        revision=outcome.revision,
        replayed=outcome.replayed,
    )


@router.post(
    "/assignment-queue",
    response_model=WorkOrderAssignmentQueueRead,
    status_code=status.HTTP_201_CREATED,
)
def create_assignment_queue_item(
    payload: WorkOrderAssignmentQueueCreate,
    access: PermissionAdmission = Depends(
        require_permission_admission("operations:dispatch:assign")
    ),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    context = _assignment_context(access, request_id=request_id)
    db_session_adapter.release_read_transaction(db)
    return dispatch_service.assignment_queue.create(
        db,
        payload,
        context=context,
        access=access,
    )


@router.get(
    "/assignment-queue",
    response_model=ListResponse[WorkOrderAssignmentQueueRead],
)
def list_assignment_queue(
    status: str | None = None,
    crm_work_order_id: str | None = None,
    assigned_technician_id: str | None = None,
    assigned_vendor_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return dispatch_service.assignment_queue.list_response(
        db,
        status=status,
        crm_work_order_id=crm_work_order_id,
        assigned_technician_id=assigned_technician_id,
        assigned_vendor_id=assigned_vendor_id,
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
    access: PermissionAdmission = Depends(
        require_permission_admission("operations:dispatch:assign")
    ),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    context = _assignment_context(access, request_id=request_id)
    db_session_adapter.release_read_transaction(db)
    return dispatch_service.assignment_queue.update(
        db,
        queue_id,
        payload,
        context=context,
        access=access,
    )


@router.post(
    "/work-orders/{work_order_id}/classification",
    response_model=WorkOrderClassificationRead,
)
def classify_work_order(
    work_order_id: UUID,
    payload: WorkOrderClassificationRequest,
    auth: dict = Depends(require_permission("operations:dispatch:write")),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
) -> WorkOrderClassificationRead:
    context = CommandContext(
        command_id=payload.command_id,
        correlation_id=uuid5(NAMESPACE_URL, request_id)
        if request_id
        else payload.command_id,
        actor=str(auth.get("principal_id") or "dispatch"),
        scope="operations:dispatch:write",
        reason=payload.reason,
        idempotency_key=str(payload.command_id),
    )
    command = ClassifyWorkOrderCommand(
        context=context,
        work_order_id=work_order_id,
        expected_revision=payload.expected_revision,
        classification=payload.classification,
    )
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = dispatch_service.work_order_commands.classify(db, command=command)
    except WorkOrderTemplateError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except WorkOrderCommandError as exc:
        raise HTTPException(
            status_code={
                "invalid": 422,
                "forbidden": 403,
                "not_found": 404,
                "conflict": 409,
            }[exc.kind],
            detail=exc.message,
        ) from exc
    return WorkOrderClassificationRead(outcome=outcome)
