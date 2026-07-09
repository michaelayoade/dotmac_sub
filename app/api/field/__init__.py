from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.devices import router as devices_router
from app.api.field.schedule import router as schedule_router
from app.schemas.common import ListResponse
from app.schemas.field import FieldJobDetail, FieldJobSummary, FieldMeResponse
from app.services.auth_dependencies import require_user_auth
from app.services.field.jobs import field_jobs

router = APIRouter(prefix="/field", tags=["field"])
router.include_router(devices_router)
router.include_router(schedule_router)


@router.get("/me", response_model=FieldMeResponse)
def field_me(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_jobs.me(db, auth)


@router.get("/jobs", response_model=ListResponse[FieldJobSummary])
def list_field_jobs(
    status: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_jobs.list(
        db,
        auth,
        status=status,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/jobs/{crm_work_order_id}", response_model=FieldJobDetail)
def get_field_job(
    crm_work_order_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_jobs.get_detail(db, auth, crm_work_order_id)
