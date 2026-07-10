from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.workqueue import (
    WorkqueueItemRead,
    WorkqueueSnoozeCreate,
    WorkqueueSnoozeRead,
)
from app.services import workqueue
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.response import list_response

router = APIRouter(prefix="/workqueue", tags=["workqueue"])


def _user_id(auth: dict) -> str:
    return str(auth.get("principal_id") or auth.get("person_id"))


@router.get(
    "",
    response_model=ListResponse[WorkqueueItemRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_workqueue(
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    rows = workqueue.list_workqueue(
        db,
        user_id=_user_id(auth),
        service_team_id=service_team_id,
        include_snoozed=include_snoozed,
        limit=limit,
        offset=offset,
    )
    return list_response(
        [WorkqueueItemRead(**row.__dict__) for row in rows], limit, offset
    )


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
    snooze = workqueue.snooze_item(
        db,
        user_id=_user_id(auth),
        item_kind=payload.item_kind,
        item_id=payload.item_id,
        snooze_until=payload.snooze_until,
        until_next_reply=payload.until_next_reply,
    )
    db.commit()
    db.refresh(snooze)
    return snooze


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
    workqueue.clear_snooze(
        db,
        user_id=_user_id(auth),
        item_kind=item_kind,
        item_id=item_id,
    )
    db.commit()
