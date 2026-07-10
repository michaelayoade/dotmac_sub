from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.team_inbox import InboxConversation, InboxConversationStatus
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
from app.schemas.team_inbox import (
    InboxConversationEscalateRequest,
    InboxConversationEscalationRead,
    InboxConversationReplyRead,
    InboxConversationReplyRequest,
    InboxConversationTimelineRead,
)
from app.services import support as support_service
from app.services import team_inbox_assignment, team_inbox_outbound, team_inbox_read
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
def create_ticket(
    payload: TicketCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return support_service.tickets.create(
        db, payload, actor_id=_actor_id(auth), request=None
    )


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
    return support_service.tickets.update(
        db, str(ticket_id), payload, actor_id=_actor_id(auth), request=None
    )


@router.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:delete"))],
)
def soft_delete_ticket(
    ticket_id: UUID, auth=Depends(require_user_auth), db: Session = Depends(get_db)
):
    support_service.tickets.soft_delete(
        db, str(ticket_id), actor_id=_actor_id(auth), request=None
    )


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
    items = support_service.tickets.bulk_update(
        db, payload, actor_id=_actor_id(auth), request=None
    )
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.post(
    "/tickets/{ticket_id}/auto-assign",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def manual_auto_assign(
    ticket_id: UUID, auth=Depends(require_user_auth), db: Session = Depends(get_db)
):
    return support_service.tickets.manual_auto_assign(
        db, str(ticket_id), actor_id=_actor_id(auth), request=None
    )


@router.post(
    "/inbox/conversations/{conversation_id}/escalate",
    response_model=InboxConversationEscalationRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def escalate_inbox_conversation(
    conversation_id: UUID,
    payload: InboxConversationEscalateRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.status == InboxConversationStatus.resolved.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resolved conversations cannot be escalated",
        )

    actor_id = _actor_id(auth)
    if payload.assigned_person_id is not None:
        result = team_inbox_assignment.assign_conversation_to_agent(
            db,
            conversation=conversation,
            service_team_id=payload.service_team_id,
            person_id=payload.assigned_person_id,
            assigned_by_person_id=actor_id,
            reason=payload.reason,
        )
    elif payload.auto_assign:
        result = team_inbox_assignment.assign_conversation_to_available_agent(
            db,
            conversation=conversation,
            service_team_id=payload.service_team_id,
            assigned_by_person_id=actor_id,
            reason=payload.reason,
        )
    else:
        result = team_inbox_assignment.queue_conversation_for_team(
            db,
            conversation=conversation,
            service_team_id=payload.service_team_id,
            assigned_by_person_id=actor_id,
            reason=payload.reason,
        )

    if result.kind in {"invalid_team", "invalid_agent"}:
        raise HTTPException(status_code=400, detail=result.reason)

    db.commit()
    return InboxConversationEscalationRead(
        conversation_id=conversation.id,
        kind=result.kind,
        service_team_id=UUID(result.service_team_id)
        if result.service_team_id
        else None,
        assigned_person_id=(
            UUID(result.assigned_person_id) if result.assigned_person_id else None
        ),
        reason=result.reason,
    )


@router.get(
    "/inbox/conversations/{conversation_id}",
    response_model=InboxConversationTimelineRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_inbox_conversation_timeline(
    conversation_id: UUID,
    db: Session = Depends(get_db),
):
    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return InboxConversationTimelineRead.model_validate(timeline, from_attributes=True)


@router.post(
    "/inbox/conversations/{conversation_id}/reply",
    response_model=InboxConversationReplyRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def reply_to_inbox_conversation(
    conversation_id: UUID,
    payload: InboxConversationReplyRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        raise HTTPException(status_code=404, detail="Conversation not found")

    result = team_inbox_outbound.send_inbox_reply(
        db,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html=payload.body_html,
            body_text=payload.body_text,
            subject=payload.subject,
            to_email=payload.to_email,
            sent_by_person_id=_actor_id(auth),
        ),
    )
    if result.kind == "invalid_conversation":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.reason,
        )
    if result.kind in {"missing_recipient", "empty_body"}:
        raise HTTPException(status_code=400, detail=result.reason)
    if result.kind == "send_failed":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.reason,
        )

    db.commit()
    return InboxConversationReplyRead(
        conversation_id=UUID(result.conversation_id),
        kind=result.kind,
        message_id=UUID(result.message_id) if result.message_id else None,
        service_team_id=UUID(result.service_team_id)
        if result.service_team_id
        else None,
        sender_key=result.sender_key,
        activity=result.activity,
        from_address=result.from_address,
        to_email=result.to_email,
        reason=result.reason,
    )


@router.post(
    "/tickets/{ticket_id}/links",
    dependencies=[
        Depends(require_permission("support:ticket:update")),
        Depends(require_agent_or_admin),
    ],
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
    dependencies=[
        Depends(require_permission("support:ticket:update")),
        Depends(require_agent_or_admin),
    ],
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
    items = support_service.ticket_comments.list(
        db, str(ticket_id), limit=limit, offset=offset
    )
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
    return support_service.ticket_comments.update(
        db, comment=comment, payload=payload, actor_id=_actor_id(auth), request=None
    )


@router.delete(
    "/tickets/{ticket_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def delete_comment(
    ticket_id: UUID,
    comment_id: UUID,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    comment = support_service.ticket_comments.get(db, str(comment_id))
    if str(comment.ticket_id) != str(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket comment not found")
    support_service.ticket_comments.delete(
        db, comment=comment, actor_id=_actor_id(auth), request=None
    )


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
    items = support_service.ticket_sla_events.list(
        db, str(ticket_id), limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.patch(
    "/sla-events/{event_id}",
    response_model=TicketSlaEventRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def update_sla_event(
    event_id: UUID, payload: TicketSlaEventUpdate, db: Session = Depends(get_db)
):
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
