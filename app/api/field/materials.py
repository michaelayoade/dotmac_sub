from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldMaterialConsumeRequest, FieldMaterialRead
from app.services.auth_dependencies import require_user_auth
from app.services.field.materials import field_materials

router = APIRouter(tags=["field-materials"])


@router.get(
    "/jobs/{crm_work_order_id}/materials", response_model=list[FieldMaterialRead]
)
def list_job_materials(
    crm_work_order_id: str,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_materials.list_for_job(db, auth, crm_work_order_id)


@router.post(
    "/jobs/{crm_work_order_id}/materials/consume",
    response_model=list[FieldMaterialRead],
)
def consume_job_materials(
    crm_work_order_id: str,
    payload: FieldMaterialConsumeRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_materials.consume(
        db,
        auth,
        crm_work_order_id,
        [item.model_dump() for item in payload.items],
    )
