from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldExpenseRequestRead,
    FieldManagerExpenseRejectRequest,
    FieldManagerJob,
    FieldManagerJobAssignRequest,
    FieldManagerMeResponse,
    FieldManagerSummary,
    FieldManagerTechniciansResponse,
)
from app.services.auth_dependencies import require_any_permission, require_permission
from app.services.field.expense_requests import field_expense_requests
from app.services.field.manager import field_manager

router = APIRouter(prefix="/manager", tags=["field-manager"])

# Manager-mode predicate (ported from CRM): any staff principal holding an
# operations read permission unlocks manager mode; writes stay behind the
# matching write/dispatch permissions.
_manager_access = require_any_permission(
    "operations:work_order:read",
    "operations:technician:read",
    "operations:expense_request:read",
)
_ops_read = require_any_permission(
    "operations:work_order:read",
    "operations:technician:read",
)
_dispatch_write = require_any_permission(
    "operations:work_order:update",
    "operations:work_order:dispatch",
)
_expense_read = require_permission("operations:expense_request:read")
_expense_write = require_permission("operations:expense_request:write")


@router.get("/me", response_model=FieldManagerMeResponse)
def field_manager_me(
    auth: dict = Depends(_manager_access),
    db: Session = Depends(get_db),
):
    return field_manager.me(db, auth)


@router.get("/summary", response_model=FieldManagerSummary)
def field_manager_summary(
    stale_after_seconds: int = Query(default=120, ge=30, le=3600),
    auth: dict = Depends(_ops_read),
    db: Session = Depends(get_db),
):
    return field_manager.summary(db, stale_after_seconds=stale_after_seconds)


@router.get("/technicians", response_model=FieldManagerTechniciansResponse)
def field_manager_technicians(
    stale_after_seconds: int = Query(default=120, ge=30, le=3600),
    limit: int = Query(default=500, ge=1, le=500),
    auth: dict = Depends(_ops_read),
    db: Session = Depends(get_db),
):
    items = field_manager.list_technicians(
        db, stale_after_seconds=stale_after_seconds, limit=limit
    )
    return {
        "items": items,
        "count": len(items),
        "live_count": sum(1 for item in items if item["is_live"]),
        "sharing_count": sum(1 for item in items if item["location_sharing_enabled"]),
        "limit": limit,
        "offset": 0,
    }


@router.get("/jobs", response_model=ListResponse[FieldManagerJob])
def field_manager_jobs(
    status: str | None = None,
    assigned_to_person_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(_ops_read),
    db: Session = Depends(get_db),
):
    items = field_manager.list_jobs(
        db,
        status=status,
        assigned_to_person_id=assigned_to_person_id,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post("/jobs/{crm_work_order_id}/assign", response_model=FieldManagerJob)
def field_manager_assign_job(
    crm_work_order_id: str,
    payload: FieldManagerJobAssignRequest,
    auth: dict = Depends(_dispatch_write),
    db: Session = Depends(get_db),
):
    return field_manager.assign_job(
        db,
        crm_work_order_id,
        person_id=payload.person_id,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        status=payload.status,
    )


@router.get("/expenses", response_model=ListResponse[FieldExpenseRequestRead])
def field_manager_expenses(
    status_filter: str | None = Query(default="submitted", alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(_expense_read),
    db: Session = Depends(get_db),
):
    items = field_expense_requests.list_all(
        db, status=status_filter, limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "/expenses/{expense_request_id}/approve",
    response_model=FieldExpenseRequestRead,
)
def field_manager_approve_expense(
    expense_request_id: str,
    auth: dict = Depends(_expense_write),
    db: Session = Depends(get_db),
):
    return field_expense_requests.approve(db, expense_request_id)


@router.post(
    "/expenses/{expense_request_id}/reject",
    response_model=FieldExpenseRequestRead,
)
def field_manager_reject_expense(
    expense_request_id: str,
    payload: FieldManagerExpenseRejectRequest,
    auth: dict = Depends(_expense_write),
    db: Session = Depends(get_db),
):
    return field_expense_requests.reject(db, expense_request_id, payload.reason)
