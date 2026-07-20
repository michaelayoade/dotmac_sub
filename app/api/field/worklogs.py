from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldWorkLogSubmit, FieldWorkLogSubmitResponse
from app.services.auth_dependencies import require_user_auth
from app.services.field.worklogs import field_worklogs

router = APIRouter(tags=["field-worklogs"])


@router.post(
    "/jobs/{crm_work_order_id}/worklogs",
    response_model=FieldWorkLogSubmitResponse,
)
def submit_field_worklogs(
    crm_work_order_id: str,
    payload: FieldWorkLogSubmit,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return {
        "results": field_worklogs.submit(
            db,
            auth,
            crm_work_order_id,
            [entry.model_dump() for entry in payload.entries],
        )
    }
