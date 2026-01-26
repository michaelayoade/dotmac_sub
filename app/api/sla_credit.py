from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.common import ListResponse
from app.schemas.sla_credit import (
    SlaCreditApplyRequest,
    SlaCreditApplyResult,
    SlaCreditItemRead,
    SlaCreditItemUpdate,
    SlaCreditReportCreate,
    SlaCreditReportRead,
    SlaCreditReportUpdate,
)
from app.services import sla_credit as sla_credit_service

router = APIRouter(prefix="/sla-credits", tags=["sla-credits"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/reports",
    response_model=SlaCreditReportRead,
    status_code=status.HTTP_201_CREATED,
)
def create_report(payload: SlaCreditReportCreate, db: Session = Depends(get_db)):
    return sla_credit_service.sla_credit_reports.create(db, payload)


@router.get("/reports/{report_id}", response_model=SlaCreditReportRead)
def get_report(report_id: str, db: Session = Depends(get_db)):
    return sla_credit_service.sla_credit_reports.get(db, report_id)


@router.get("/reports", response_model=ListResponse[SlaCreditReportRead])
def list_reports(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sla_credit_service.sla_credit_reports.list_response(
        db, status, order_by, order_dir, limit, offset
    )


@router.patch("/reports/{report_id}", response_model=SlaCreditReportRead)
def update_report(
    report_id: str, payload: SlaCreditReportUpdate, db: Session = Depends(get_db)
):
    return sla_credit_service.sla_credit_reports.update(db, report_id, payload)


@router.post(
    "/reports/{report_id}/apply",
    response_model=SlaCreditApplyResult,
)
def apply_report(
    report_id: str, payload: SlaCreditApplyRequest, db: Session = Depends(get_db)
):
    return sla_credit_service.sla_credit_reports.apply(db, report_id, payload)


@router.get("/items/{item_id}", response_model=SlaCreditItemRead)
def get_item(item_id: str, db: Session = Depends(get_db)):
    return sla_credit_service.sla_credit_items.get(db, item_id)


@router.get("/items", response_model=ListResponse[SlaCreditItemRead])
def list_items(
    report_id: str | None = None,
    account_id: str | None = None,
    approved: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return sla_credit_service.sla_credit_items.list_response(
        db, report_id, account_id, approved, order_by, order_dir, limit, offset
    )


@router.patch("/items/{item_id}", response_model=SlaCreditItemRead)
def update_item(
    item_id: str, payload: SlaCreditItemUpdate, db: Session = Depends(get_db)
):
    return sla_credit_service.sla_credit_items.update(db, item_id, payload)
