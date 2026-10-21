from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.execution import (
    field_command_context,
    field_domain_errors,
    field_system_user_id,
)
from app.schemas.field import FieldWorkLogSubmit, FieldWorkLogSubmitResponse
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.execution_contracts import (
    FieldWorkLogEntry,
    SubmitFieldWorkLogs,
)
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
    principal_id = field_system_user_id(auth)
    with field_domain_errors():
        db_session_adapter.release_read_transaction(db)
        results = field_worklogs.submit(
            db=db,
            command=SubmitFieldWorkLogs(
                context=field_command_context(
                    principal_id, reason="field_worklog_submission"
                ),
                requester_system_user_id=principal_id,
                public_id=crm_work_order_id,
                entries=tuple(
                    FieldWorkLogEntry(**entry.model_dump()) for entry in payload.entries
                ),
            ),
        )
    return {"results": results}
