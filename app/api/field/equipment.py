from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldEquipmentCustodyRead,
    FieldEquipmentRead,
    FieldEquipmentRecord,
)
from app.services.auth_dependencies import require_user_auth
from app.services.field.equipment import field_equipment
from app.services.field.equipment_custody import field_equipment_custody

router = APIRouter(tags=["field-equipment"])


@router.post(
    "/jobs/{crm_work_order_id}/equipment",
    response_model=FieldEquipmentRead,
    status_code=status.HTTP_201_CREATED,
)
def record_job_equipment(
    crm_work_order_id: str,
    payload: FieldEquipmentRecord,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_equipment.record(
        db,
        auth,
        crm_work_order_id,
        serial_number=payload.serial_number,
        vendor=payload.vendor,
        model=payload.model,
        notes=payload.notes,
    )


@router.get(
    "/jobs/{crm_work_order_id}/equipment", response_model=FieldEquipmentRead | None
)
def get_job_equipment(
    crm_work_order_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_equipment.current_for_job(db, auth, crm_work_order_id)


@router.get(
    "/equipment-custody/mine",
    response_model=ListResponse[FieldEquipmentCustodyRead],
)
def list_my_equipment_custody(
    status_filter: str = Query(default="issued", alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = field_equipment_custody.list_mine(
        db,
        auth,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}
