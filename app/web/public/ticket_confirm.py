"""Public ticket resolution confirmation pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import support as support_service
from app.web.customer.branding import get_customer_templates

router = APIRouter(prefix="/ticket-confirm", tags=["public-ticket-confirm"])
templates = get_customer_templates()


def _ticket_summary(token_row) -> dict[str, object]:
    ticket = getattr(token_row, "ticket", None)
    if ticket is None:
        return {}
    return {
        "ticket_id": str(ticket.id),
        "ticket_ref": ticket.number or str(ticket.id),
        "subject": ticket.title,
        "status": ticket.status,
        "resolved_at": ticket.resolved_at,
    }


def _render(
    request: Request,
    *,
    token: str,
    state: str,
    token_row=None,
    message: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        "public/ticket_confirm.html",
        {
            "request": request,
            "token": token,
            "state": state,
            "message": message,
            "ticket": _ticket_summary(token_row) if token_row is not None else {},
        },
        status_code=status_code,
    )


def _load_token(db: Session, token: str):
    token_row = support_service.ticket_access_tokens.get_by_token(db, token)
    state = support_service.ticket_access_tokens.token_state(token_row)
    return token_row, state


@router.get("/{token}", response_class=HTMLResponse)
def confirmation_page(request: Request, token: str, db: Session = Depends(get_db)):
    token_row, state = _load_token(db, token)
    if state == "ok":
        assert token_row is not None
        support_service.ticket_access_tokens.mark_accessed(db, token_row)
        return _render(request, token=token, state="ok", token_row=token_row)
    status_code = {"not_found": 404, "expired": 410, "closed": 409}.get(state, 400)
    return _render(
        request,
        token=token,
        state=state,
        token_row=token_row,
        status_code=status_code,
    )


@router.post("/{token}/confirm", response_class=HTMLResponse)
def confirm_resolution_page(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):
    token_row, state = _load_token(db, token)
    if state != "ok":
        status_code = {"not_found": 404, "expired": 410, "closed": 409}.get(state, 400)
        return _render(
            request,
            token=token,
            state=state,
            token_row=token_row,
            status_code=status_code,
        )
    assert token_row is not None
    try:
        ticket = support_service.tickets.confirm_resolution(db, token_row)
    except HTTPException as exc:
        db.rollback()
        return _render(
            request,
            token=token,
            state="error",
            token_row=token_row,
            message=str(exc.detail),
            status_code=exc.status_code,
        )
    token_row.ticket = ticket
    return _render(request, token=token, state="confirmed", token_row=token_row)


@router.post("/{token}/dispute", response_class=HTMLResponse)
def dispute_resolution_page(
    request: Request,
    token: str,
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    token_row, state = _load_token(db, token)
    if state != "ok":
        status_code = {"not_found": 404, "expired": 410, "closed": 409}.get(state, 400)
        return _render(
            request,
            token=token,
            state=state,
            token_row=token_row,
            status_code=status_code,
        )
    assert token_row is not None
    try:
        ticket = support_service.tickets.dispute_resolution(
            db,
            token_row,
            reason=reason,
        )
    except HTTPException as exc:
        db.rollback()
        return _render(
            request,
            token=token,
            state="error",
            token_row=token_row,
            message=str(exc.detail),
            status_code=exc.status_code,
        )
    token_row.ticket = ticket
    return _render(request, token=token, state="disputed", token_row=token_row)
