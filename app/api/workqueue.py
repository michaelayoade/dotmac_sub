from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.workqueue import (
    WorkqueueItemRead,
    WorkqueueSnoozeCreate,
    WorkqueueSnoozeRead,
    WorkqueueViewRead,
)
from app.services import workqueue
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.response import list_response
from app.services.workqueue import WorkqueuePermissionError, WorkqueuePrincipal

router = APIRouter(prefix="/workqueue", tags=["workqueue"])

AUDIENCE_QUERY = Query(
    default=None,
    description="self | team | org — clamped to the audience the caller holds",
)


def _user_id(auth: dict) -> str:
    return str(auth.get("principal_id") or auth.get("person_id"))


def _principal(db: Session, auth: dict) -> WorkqueuePrincipal:
    try:
        return workqueue.principal_from_auth(db, auth)
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get(
    "",
    response_model=ListResponse[WorkqueueItemRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_workqueue(
    audience: str | None = AUDIENCE_QUERY,
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    try:
        rows = workqueue.list_workqueue(
            db,
            _principal(db, auth),
            requested_audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
            limit=limit,
            offset=offset,
        )
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return list_response(
        [WorkqueueItemRead.model_validate(row) for row in rows], limit, offset
    )


@router.get(
    "/view",
    response_model=WorkqueueViewRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def workqueue_view(
    audience: str | None = AUDIENCE_QUERY,
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    """Ranked hero band ("right now") plus one section per item source."""
    try:
        view = workqueue.build_workqueue(
            db,
            _principal(db, auth),
            requested_audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
        )
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return WorkqueueViewRead.model_validate(view)


@router.post(
    "/snoozes",
    response_model=WorkqueueSnoozeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def snooze_item(
    payload: WorkqueueSnoozeCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return workqueue.snooze_item_committed(
        db,
        user_id=_user_id(auth),
        item_kind=payload.item_kind,
        item_id=payload.item_id,
        snooze_until=payload.snooze_until,
        until_next_reply=payload.until_next_reply,
    )


@router.delete(
    "/snoozes/{item_kind}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def clear_snooze(
    item_kind: str,
    item_id: UUID,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    workqueue.clear_snooze_committed(
        db,
        user_id=_user_id(auth),
        item_kind=item_kind,
        item_id=item_id,
    )
