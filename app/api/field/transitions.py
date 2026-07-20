from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import FieldTransitionRequest, FieldTransitionResponse
from app.services.auth_dependencies import require_user_auth
from app.services.field.jobs import _summary
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
    result = field_transitions.apply(
        db,
        auth,
        crm_work_order_id,
        event=payload.event,
        client_event_id=payload.client_event_id,
        occurred_at=payload.occurred_at,
        latitude=payload.latitude,
        longitude=payload.longitude,
        note=payload.note,
        payload=payload.payload,
    )
    return {
        "job": _summary(result["job"]),
        "event": result["event"],
        "replayed": result["replayed"],
    }
