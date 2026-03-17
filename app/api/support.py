from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.support import (
    TicketBulkUpdateRequest,
    TicketCommentCreate,
    TicketCommentRead,
    TicketCommentUpdate,
    TicketCreate,
    TicketLinkCreate,
    TicketMergeRequest,
    TicketRead,
    TicketSlaEventCreate,
    TicketSlaEventRead,
    TicketSlaEventUpdate,
    TicketUpdate,
)
from app.services import support as support_service
from app.services.auth_dependencies import require_permission, require_user_auth

router = APIRouter(prefix="/support", tags=["support"])


def _actor_id(auth: dict) -> str | None:
    principal = auth.get("principal_id")
    return str(principal) if principal else None


def require_agent_or_admin(auth=Depends(require_user_auth)):
    roles = {str(role).lower() for role in (auth.get("roles") or [])}
    if "admin" in roles or "agent" in roles or "support" in roles:
        return auth
    raise HTTPException(status_code=403, detail="Forbidden")


@router.post(
    "/tickets",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def create_ticket(payload: TicketCreate, auth=Depends(require_user_auth), db: Session = Depends(get_db)):
    return support_service.tickets.create(db, payload, actor_id=_actor_id(auth), request=None)


@router.get(
    "/tickets",
    response_model=ListResponse[TicketRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_tickets(
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    assigned_to_person_id: str | None = Query(default=None),
    project_manager_person_id: str | None = Query(default=None),
    site_coordinator_person_id: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    ):
    return support_service.tickets.list_response(
        db,
        search,
        status,
        ticket_type,
        assigned_to_person_id,
        project_manager_person_id,
        site_coordinator_person_id,
        subscriber_id,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.get(
    "/tickets/lookup/{ticket_lookup}",
    response_model=TicketRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_ticket(ticket_lookup: str, db: Session = Depends(get_db)):
    return support_service.tickets.get_by_lookup(db, ticket_lookup)


@router.get(
    "/tickets/{ticket_lookup}",
    response_model=TicketRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_ticket_legacy_path(ticket_lookup: str, db: Session = Depends(get_db)):
    return support_service.tickets.get_by_lookup(db, ticket_lookup)


@router.patch(
    "/tickets/{ticket_id}",
    response_model=TicketRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def update_ticket(
    ticket_id: UUID,
    payload: TicketUpdate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return support_service.tickets.update(db, str(ticket_id), payload, actor_id=_actor_id(auth), request=None)


@router.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:delete"))],
)
def soft_delete_ticket(ticket_id: UUID, auth=Depends(require_user_auth), db: Session = Depends(get_db)):
    support_service.tickets.soft_delete(db, str(ticket_id), actor_id=_actor_id(auth), request=None)


@router.post(
    "/tickets/bulk-update",
    response_model=ListResponse[TicketRead],
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def bulk_update_tickets(
    payload: TicketBulkUpdateRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = support_service.tickets.bulk_update(db, payload, actor_id=_actor_id(auth), request=None)
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.post(
    "/tickets/{ticket_id}/auto-assign",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def manual_auto_assign(ticket_id: UUID, auth=Depends(require_user_auth), db: Session = Depends(get_db)):
    return support_service.tickets.manual_auto_assign(db, str(ticket_id), actor_id=_actor_id(auth), request=None)


@router.post(
    "/tickets/{ticket_id}/links",
    dependencies=[Depends(require_permission("support:ticket:update")), Depends(require_agent_or_admin)],
)
def create_ticket_link(
    ticket_id: UUID,
    payload: TicketLinkCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return support_service.tickets.link_ticket(
        db,
        from_ticket_id=str(ticket_id),
        to_ticket_id=str(payload.to_ticket_id),
        link_type=payload.link_type,
        actor_id=_actor_id(auth),
        request=None,
    )


@router.post(
    "/tickets/{ticket_id}/merge",
    response_model=TicketRead,
    dependencies=[Depends(require_permission("support:ticket:update")), Depends(require_agent_or_admin)],
)
def merge_ticket(
    ticket_id: UUID,
    payload: TicketMergeRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return support_service.tickets.merge(
        db,
        str(ticket_id),
        payload,
        actor_id=_actor_id(auth),
        request=None,
    )


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=TicketCommentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def create_comment(
    ticket_id: UUID,
    payload: TicketCommentCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return support_service.tickets.create_comment(
        db,
        str(ticket_id),
        payload,
        actor_id=_actor_id(auth),
        request=None,
    )


@router.post(
    "/tickets/{ticket_id}/comments/bulk",
    response_model=ListResponse[TicketCommentRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def bulk_create_comments(
    ticket_id: UUID,
    payload: list[TicketCommentCreate],
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    items = support_service.tickets.bulk_create_comments(
        db,
        str(ticket_id),
        payload,
        actor_id=_actor_id(auth),
        request=None,
    )
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.get(
    "/tickets/{ticket_id}/comments",
    response_model=ListResponse[TicketCommentRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_comments(
    ticket_id: UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = support_service.ticket_comments.list(db, str(ticket_id), limit=limit, offset=offset)
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get(
    "/tickets/{ticket_id}/comments/{comment_id}",
    response_model=TicketCommentRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_comment(ticket_id: UUID, comment_id: UUID, db: Session = Depends(get_db)):
    comment = support_service.ticket_comments.get(db, str(comment_id))
    if str(comment.ticket_id) != str(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket comment not found")
    return comment


@router.patch(
    "/tickets/{ticket_id}/comments/{comment_id}",
    response_model=TicketCommentRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def update_comment(
    ticket_id: UUID,
    comment_id: UUID,
    payload: TicketCommentUpdate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    comment = support_service.ticket_comments.get(db, str(comment_id))
    if str(comment.ticket_id) != str(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket comment not found")
    return support_service.ticket_comments.update(db, comment=comment, payload=payload, actor_id=_actor_id(auth), request=None)


@router.delete(
    "/tickets/{ticket_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def delete_comment(ticket_id: UUID, comment_id: UUID, auth=Depends(require_user_auth), db: Session = Depends(get_db)):
    comment = support_service.ticket_comments.get(db, str(comment_id))
    if str(comment.ticket_id) != str(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket comment not found")
    support_service.ticket_comments.delete(db, comment=comment, actor_id=_actor_id(auth), request=None)


@router.post(
    "/sla-events",
    response_model=TicketSlaEventRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def create_sla_event(payload: TicketSlaEventCreate, db: Session = Depends(get_db)):
    return support_service.ticket_sla_events.create(db, payload)


@router.get(
    "/sla-events/{event_id}",
    response_model=TicketSlaEventRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_sla_event(event_id: UUID, db: Session = Depends(get_db)):
    return support_service.ticket_sla_events.get(db, str(event_id))


@router.get(
    "/sla-events",
    response_model=ListResponse[TicketSlaEventRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_sla_events(
    ticket_id: UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = support_service.ticket_sla_events.list(db, str(ticket_id), limit=limit, offset=offset)
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.patch(
    "/sla-events/{event_id}",
    response_model=TicketSlaEventRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def update_sla_event(event_id: UUID, payload: TicketSlaEventUpdate, db: Session = Depends(get_db)):
    event = support_service.ticket_sla_events.get(db, str(event_id))
    return support_service.ticket_sla_events.update(db, event, payload)


@router.delete(
    "/sla-events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def delete_sla_event(event_id: UUID, db: Session = Depends(get_db)):
    event = support_service.ticket_sla_events.get(db, str(event_id))
    support_service.ticket_sla_events.delete(db, event)
