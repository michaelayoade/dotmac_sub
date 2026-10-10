from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.execution import (
    field_command_context,
    field_domain_errors,
    field_system_user_id,
)
from app.schemas.field import FieldTransitionRequest, FieldTransitionResponse
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.execution_contracts import (
    ApplyFieldTransition,
    FieldEvent,
    FieldTransitionPayload,
)
from app.services.field.transitions import field_transitions

router = APIRouter(tags=["field-transitions"])


@router.post(
    "/jobs/{crm_work_order_id}/transition",
    response_model=FieldTransitionResponse,
    status_code=status.HTTP_201_CREATED,
)
def transition_field_job(
    crm_work_order_id: str,
    payload: FieldTransitionRequest,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    principal_id = field_system_user_id(auth)
    with field_domain_errors():
        db_session_adapter.release_read_transaction(db)
        return field_transitions.apply(
            db=db,
            command=ApplyFieldTransition(
                context=field_command_context(
                    principal_id,
                    reason="field_job_transition",
                    request_id=payload.client_event_id,
                ),
                requester_system_user_id=principal_id,
                public_id=crm_work_order_id,
                event=FieldEvent(payload.event),
                client_event_id=payload.client_event_id,
                occurred_at=payload.occurred_at,
                latitude=payload.latitude,
                longitude=payload.longitude,
                note=payload.note,
                payload=FieldTransitionPayload(**payload.payload.model_dump()),
            ),
        )
