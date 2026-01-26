from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.schemas.common import ListResponse

from app.db import SessionLocal
from app.schemas.audit import AuditEventRead
from app.services import audit as audit_service
from app.api.deps import require_audit_auth

router = APIRouter(
    prefix="/audit-events",
    tags=["audit-events"],
    dependencies=[Depends(require_audit_auth)],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/{event_id}", response_model=AuditEventRead)
def get_audit_event(event_id: str, db: Session = Depends(get_db)):
    return audit_service.audit_events.get(db, event_id)


@router.get("", response_model=ListResponse[AuditEventRead])
def list_audit_events(
    actor_id: str | None = None,
    actor_type: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    request_id: str | None = None,
    is_success: bool | None = None,
    status_code: int | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="occurred_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    resolved_actor_type = audit_service.audit_events.parse_actor_type(actor_type)
    return audit_service.audit_events.list_response(
        db,
        actor_id,
        resolved_actor_type,
        action,
        entity_type,
        request_id,
        is_success,
        status_code,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.delete(
    "/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_audit_event(event_id: str, db: Session = Depends(get_db)):
    audit_service.audit_events.delete(db, event_id)