"""Staff resolution-confirmation transport; Support owns readiness and consent."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.field.execution import field_domain_errors
from app.csrf import require_browser_csrf_header
from app.db import get_db
from app.schemas.support_resolution import PERMISSION, ResolutionRequestInput
from app.services import support
from app.services.auth_dependencies import require_permission_admission
from app.services.db_session_adapter import db_session_adapter
from app.services.permission_admission import PermissionAdmission
from app.services.support_resolution_contracts import (
    SupportResolutionPreview,
    SupportResolutionPreviewQuery,
    SupportResolutionRequestOutcome,
)

router = APIRouter(prefix="/support/tickets", tags=["support"])


@router.get("/{ticket_id}/resolution-confirmation")
def preview(
    ticket_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> SupportResolutionPreview:
    with field_domain_errors():
        return support.resolution_request_preview(
            db, query=SupportResolutionPreviewQuery(ticket_id=ticket_id, access=access)
        )


@router.post(
    "/{ticket_id}/resolution-confirmation",
    dependencies=[Depends(require_browser_csrf_header)],
)
def request_confirmation(
    ticket_id: UUID,
    payload: ResolutionRequestInput,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> SupportResolutionRequestOutcome:
    command = payload.command(ticket_id=ticket_id, access=access)
    db_session_adapter.release_read_transaction(db)
    with field_domain_errors():
        return support.request_resolution(db, command=command)
