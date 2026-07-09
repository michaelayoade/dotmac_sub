from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldEquipmentRead, FieldEquipmentRecord
from app.services.auth_dependencies import require_user_auth
from app.services.field.equipment import field_equipment

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
