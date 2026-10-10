from uuid import uuid4

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.execution import field_domain_errors, field_system_user_id
from app.schemas.field import FieldNoteCreate, FieldNoteRead
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.note_commands import (
    CreateFieldWorkOrderNote,
    create_field_work_order_note,
)
from app.services.owner_commands import CommandContext

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
    request_id = payload.client_ref or uuid4()
    principal_id = field_system_user_id(auth)
    with field_domain_errors():
        db_session_adapter.release_read_transaction(db)
        return create_field_work_order_note(
            db=db,
            command=CreateFieldWorkOrderNote(
                context=CommandContext(
                    command_id=request_id,
                    correlation_id=request_id,
                    actor=f"user:{principal_id}",
                    scope="field:work_order_notes:write",
                    reason="field_work_order_note_creation",
                    idempotency_key=str(request_id),
                ),
                requester_system_user_id=principal_id,
                work_order_public_id=crm_work_order_id,
                request_id=request_id,
                body=payload.body,
                is_internal=payload.is_internal,
                attachment_ids=tuple(payload.attachment_ids),
            ),
        )
