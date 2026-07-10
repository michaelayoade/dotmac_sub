from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import team_inbox_widget

router = APIRouter(prefix="/widget", tags=["chat-widget"])


class WidgetMessageCreate(BaseModel):
    body: str
    client_message_id: str | None = None


class WidgetSatisfactionCreate(BaseModel):
    rating: int
    comment: str | None = None


def _principal(
    db: Session,
    x_visitor_token: str | None,
) -> team_inbox_widget.WidgetPrincipal:
    return team_inbox_widget.decode_widget_token(db, x_visitor_token or "")


@router.get("/session/{session_id}/messages")
def widget_session_messages(
    session_id: str,
    limit: int = 50,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    principal = _principal(db, x_visitor_token)
    if principal.session_id != session_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Session mismatch")
    return team_inbox_widget.list_session_messages(
        db,
        principal=principal,
        limit=limit,
    )


@router.post("/session/{session_id}/message")
def widget_session_message_create(
    session_id: str,
    payload: WidgetMessageCreate,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    principal = _principal(db, x_visitor_token)
    if principal.session_id != session_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Session mismatch")
    result = team_inbox_widget.add_visitor_message(
        db,
        principal=principal,
        body=payload.body,
        client_message_id=payload.client_message_id,
    )
    db.commit()
    return result


@router.post("/session/{session_id}/read")
def widget_session_read(
    session_id: str,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    principal = _principal(db, x_visitor_token)
    if principal.session_id != session_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Session mismatch")
    result = team_inbox_widget.mark_session_read(db, principal=principal)
    db.commit()
    return result


@router.post("/session/{session_id}/satisfaction")
def widget_session_satisfaction(
    session_id: str,
    payload: WidgetSatisfactionCreate,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    from fastapi import HTTPException

    from app.models.team_inbox import InboxConversation
    from app.services import team_inbox_operations

    principal = _principal(db, x_visitor_token)
    if principal.session_id != session_id:
        raise HTTPException(status_code=403, detail="Session mismatch")
    conversation = db.get(InboxConversation, principal.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    team_inbox_operations.set_satisfaction(
        db,
        conversation=conversation,
        rating=payload.rating,
        comment=payload.comment,
        actor=principal.subscriber_id or principal.reseller_id or principal.session_id,
    )
    db.commit()
    return {"ok": True}
