from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

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
from app.services.db_session_adapter import db_session_adapter
from app.services.realtime_platform import (
    iter_topic_events,
    ready_event,
    reset_event,
    sse_message,
)
from app.services.response import list_response
from app.services.workqueue import WorkqueuePermissionError, WorkqueuePrincipal
from app.services.workqueue.events import channels_for_scope

router = APIRouter(prefix="/workqueue", tags=["workqueue"])
logger = logging.getLogger(__name__)

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


@router.get(
    "/events",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def workqueue_events(
    request: Request,
    audience: str | None = AUDIENCE_QUERY,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    """SSE transport for the same server-scoped workqueue invalidations as WS."""
    try:
        scope = workqueue.get_workqueue_scope(
            db,
            _principal(db, auth),
            requested_audience=audience,
        )
        topics = channels_for_scope(scope)
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Streaming responses keep dependencies alive until disconnect. Release
    # this lookup session before returning; the event stream needs no database.
    db_session_adapter.release_read_transaction(db)
    db.close()

    async def event_generator():
        yield sse_message(ready_event(topics, transport="sse"))
        if last_event_id:
            yield sse_message(reset_event(topics, reason="redis_pubsub_has_no_replay"))
        try:
            async for event in iter_topic_events(
                topics,
                stop_requested=request.is_disconnected,
            ):
                yield sse_message(event)
        except Exception as exc:
            logger.warning("workqueue_sse_stream_failed error=%s", exc)
            yield sse_message(reset_event(topics, reason="broker_unavailable"))

    return EventSourceResponse(
        event_generator(),
        ping=15,
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
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
