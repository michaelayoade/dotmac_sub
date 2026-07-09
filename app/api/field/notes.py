from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldNoteCreate, FieldNoteRead
from app.services.auth_dependencies import require_user_auth
from app.services.field.notes import field_notes

router = APIRouter(tags=["field-notes"])


@router.post(
    "/jobs/{crm_work_order_id}/notes",
    response_model=FieldNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_field_note(
    crm_work_order_id: str,
    payload: FieldNoteCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_notes.create(
        db,
        auth,
        crm_work_order_id,
        body=payload.body,
        is_internal=payload.is_internal,
        attachment_ids=[str(item) for item in payload.attachment_ids],
    )
