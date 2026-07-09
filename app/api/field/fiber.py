from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldFiberTestCreate,
    FieldFiberTestRead,
    FieldSpliceCreate,
    FieldSpliceProposalResponse,
)
from app.services.auth_dependencies import require_user_auth
from app.services.field import fiber as field_fiber

router = APIRouter(prefix="/fiber", tags=["field-fiber"])


@router.post(
    "/splices",
    response_model=FieldSpliceProposalResponse,
    status_code=status.HTTP_201_CREATED,
)
def propose_field_splice(
    payload: FieldSpliceCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_fiber.propose_splice(
        db,
        auth,
        closure_id=str(payload.closure_id),
        from_strand_id=str(payload.from_strand_id),
        to_strand_id=str(payload.to_strand_id),
        tray_id=str(payload.tray_id) if payload.tray_id else None,
        position=payload.position,
        splice_type=payload.splice_type,
        loss_db=payload.loss_db,
        note=payload.note,
    )


@router.post(
    "/tests",
    response_model=FieldFiberTestRead,
    status_code=status.HTTP_201_CREATED,
)
def record_field_fiber_test(
    payload: FieldFiberTestCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_fiber.record_test(
        db,
        auth,
        crm_work_order_id=payload.crm_work_order_id,
        asset_type=payload.asset_type,
        asset_id=str(payload.asset_id),
        test_type=payload.test_type,
        wavelength_nm=payload.wavelength_nm,
        value_db=payload.value_db,
        unit=payload.unit,
        passed=payload.passed,
        instrument=payload.instrument,
        measured_at=payload.measured_at,
        notes=payload.notes,
        attachment_id=str(payload.attachment_id) if payload.attachment_id else None,
        client_ref=str(payload.client_ref) if payload.client_ref else None,
    )


@router.get("/tests", response_model=ListResponse[FieldFiberTestRead])
def list_field_fiber_tests(
    crm_work_order_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_fiber.list_tests(db, auth, crm_work_order_id=crm_work_order_id)
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}
