"""Thin staff adapter for the implementation-to-CX handoff queue."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_permission
from app.schemas.customer_experience import (
    CustomerExperienceAcceptRequest,
    CustomerExperienceAttentionRequest,
    CustomerExperienceHandoffRead,
)
from app.services import customer_experience_handoffs

router = APIRouter(prefix="/customer-experience/handoffs", tags=["customer-experience"])


def _actor(principal: dict) -> str:
    return str(
        principal.get("user_id")
        or principal.get("principal_id")
        or principal.get("subscriber_id")
        or "authenticated-user"
    )


def _translate(exc: customer_experience_handoffs.CustomerExperienceHandoffError):
    status_code = {"not_found": 404, "invalid": 422}.get(exc.kind, 409)
    raise HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.get(
    "",
    response_model=list[CustomerExperienceHandoffRead],
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def list_handoffs(
    handoff_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    try:
        return customer_experience_handoffs.list_handoffs(
            db, status=handoff_status, limit=limit, offset=offset
        )
    except customer_experience_handoffs.CustomerExperienceHandoffError as exc:
        _translate(exc)


@router.post(
    "/{handoff_id}/accept",
    response_model=CustomerExperienceHandoffRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def accept_handoff(
    handoff_id: UUID,
    payload: CustomerExperienceAcceptRequest,
    principal: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return customer_experience_handoffs.accept_handoff(
            db,
            handoff_id=handoff_id,
            actor_type="staff_user",
            actor_id=_actor(principal),
            reason=payload.reason,
        )
    except customer_experience_handoffs.CustomerExperienceHandoffError as exc:
        _translate(exc)


@router.post(
    "/{handoff_id}/needs-attention",
    response_model=CustomerExperienceHandoffRead,
    dependencies=[Depends(require_permission("provisioning:write"))],
)
def mark_needs_attention(
    handoff_id: UUID,
    payload: CustomerExperienceAttentionRequest,
    principal: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return customer_experience_handoffs.mark_needs_attention(
            db,
            handoff_id=handoff_id,
            actor_type="staff_user",
            actor_id=_actor(principal),
            reason=payload.reason,
        )
    except customer_experience_handoffs.CustomerExperienceHandoffError as exc:
        _translate(exc)
