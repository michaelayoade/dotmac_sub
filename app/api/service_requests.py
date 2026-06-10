"""Admin queue for reseller service requests (new connections / installs)."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/service-requests", tags=["service-requests"])


class ServiceRequestStatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=20)
    admin_notes: str | None = Field(default=None, max_length=2000)


@router.get("", dependencies=[Depends(require_permission("provisioning:read"))])
def list_service_requests(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    from app.services import reseller_service_requests

    return {"items": reseller_service_requests.list_admin(db, status, limit, offset)}


@router.patch(
    "/{request_id}",
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def update_service_request(
    request_id: str,
    payload: ServiceRequestStatusUpdate,
    db: Session = Depends(get_db),
) -> dict:
    """Move a request through the queue; the reseller is notified on change."""
    from app.services import reseller_service_requests

    return reseller_service_requests.update_status(
        db, request_id, status=payload.status, admin_notes=payload.admin_notes
    )
