"""Admin team inbox routes."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import Reseller, Subscriber
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import (
    team_inbox_contact_links,
    team_inbox_metrics,
    team_inbox_operations,
    team_inbox_outbound,
    team_inbox_read,
)
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/inbox", tags=["web-admin-inbox"])
templates = Jinja2Templates(directory="templates")


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "team-inbox",
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_queue(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    channel_type: str | None = Query(default=None),
    service_team_id: str | None = Query(default=None),
    assigned_person_id: str | None = Query(default=None),
    needs_response: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page
    result = team_inbox_read.list_conversations(
        db,
        search=search,
        status=status,
        channel_type=channel_type,
        service_team_id=service_team_id,
        assigned_person_id=assigned_person_id,
        needs_response=needs_response,
        limit=per_page,
        offset=offset,
    )
    context = _ctx(request, db)
    context.update(
        {
            "rows": result.items,
            "count": result.count,
            "page": page,
            "per_page": per_page,
            "has_previous": page > 1,
            "has_next": offset + len(result.items) < result.count,
            "search": search or "",
            "status": status or "",
            "channel_type": channel_type or "",
            "service_team_id": service_team_id or "",
            "assigned_person_id": assigned_person_id or "",
            "needs_response": needs_response,
            "service_team_options": team_inbox_metrics.active_service_team_options(db),
            "status_options": [item.value for item in InboxConversationStatus],
            "channel_options": [item.value for item in InboxChannelType],
        }
    )
    return templates.TemplateResponse("admin/inbox/index.html", context)


def _detail_redirect(
    conversation_id: str | UUID,
    *,
    status: str,
    message: str,
) -> RedirectResponse:
    return RedirectResponse(
        url=(
            f"/admin/inbox/{conversation_id}?status={quote_plus(status)}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


def _candidate_terms(timeline: team_inbox_read.InboxConversationTimeline) -> list[str]:
    values = [
        timeline.contact_address,
        timeline.subject,
        timeline.external_thread_id,
    ]
    if timeline.metadata:
        resolution = timeline.metadata.get("contact_resolution")
        if isinstance(resolution, dict):
            values.extend(
                [
                    resolution.get("normalized_contact"),
                    resolution.get("subscriber_id"),
                    resolution.get("reseller_id"),
                ]
            )
    terms: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if len(text) >= 3 and text not in terms:
            terms.append(text)
    return terms[:6]


def _subscriber_label(row: Subscriber) -> str:
    full_name = " ".join(
        part for part in [row.first_name, row.last_name] if part
    ).strip()
    label = (
        row.display_name or row.company_name or full_name or row.email or str(row.id)
    )
    extras = [
        row.account_number,
        row.subscriber_number,
        row.email,
        row.phone,
        getattr(row.status, "value", row.status),
    ]
    suffix = " · ".join(str(item) for item in extras if item)
    return f"{label} ({suffix})" if suffix else label


def _reseller_label(row: Reseller) -> str:
    extras = [row.code, row.contact_email, row.contact_phone]
    suffix = " · ".join(str(item) for item in extras if item)
    return f"{row.name} ({suffix})" if suffix else row.name


def _contact_link_candidates(
    db: Session,
    timeline: team_inbox_read.InboxConversationTimeline,
) -> dict[str, list[dict[str, str]]]:
    terms = _candidate_terms(timeline)
    subscribers: list[Subscriber] = []
    resellers: list[Reseller] = []
    if terms:
        subscriber_filters = []
        reseller_filters = []
        for term in terms:
            like = f"%{term}%"
            subscriber_filters.extend(
                [
                    Subscriber.email.ilike(like),
                    Subscriber.phone.ilike(like),
                    Subscriber.first_name.ilike(like),
                    Subscriber.last_name.ilike(like),
                    Subscriber.display_name.ilike(like),
                    Subscriber.company_name.ilike(like),
                    Subscriber.account_number.ilike(like),
                    Subscriber.subscriber_number.ilike(like),
                ]
            )
            reseller_filters.extend(
                [
                    Reseller.name.ilike(like),
                    Reseller.code.ilike(like),
                    Reseller.contact_email.ilike(like),
                    Reseller.contact_phone.ilike(like),
                ]
            )
        subscribers = (
            db.query(Subscriber)
            .filter(Subscriber.is_active.is_(True))
            .filter(or_(*subscriber_filters))
            .order_by(Subscriber.updated_at.desc().nullslast())
            .limit(8)
            .all()
        )
        resellers = (
            db.query(Reseller)
            .filter(Reseller.is_active.is_(True))
            .filter(or_(*reseller_filters))
            .order_by(Reseller.name.asc())
            .limit(8)
            .all()
        )
    if not subscribers:
        subscribers = (
            db.query(Subscriber)
            .filter(Subscriber.is_active.is_(True))
            .order_by(Subscriber.updated_at.desc().nullslast())
            .limit(8)
            .all()
        )
    if not resellers:
        resellers = (
            db.query(Reseller)
            .filter(Reseller.is_active.is_(True))
            .order_by(Reseller.name.asc())
            .limit(8)
            .all()
        )
    return {
        "subscribers": [
            {"id": str(row.id), "label": _subscriber_label(row)} for row in subscribers
        ],
        "resellers": [
            {"id": str(row.id), "label": _reseller_label(row)} for row in resellers
        ],
    }


@router.get(
    "/{conversation_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_detail(
    conversation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    context = _ctx(request, db)
    context.update(
        {
            "timeline": timeline,
            "contact_link_candidates": _contact_link_candidates(db, timeline),
            "label_options": team_inbox_operations.list_labels(db),
            "conversation_labels": team_inbox_operations.conversation_labels(
                db, conversation_id
            ),
            "macro_options": team_inbox_operations.list_macros(
                db, person_id=_actor_id_from_request(request)
            ),
        }
    )
    return templates.TemplateResponse("admin/inbox/detail.html", context)


def _actor_id_from_request(request: Request) -> str | None:
    from app.services import web_admin as web_admin_service

    return web_admin_service.get_actor_id(request)


@router.post(
    "/{conversation_id}/reply",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_reply(
    conversation_id: UUID,
    request: Request,
    body_text: str = Form(...),
    macro_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    from app.services import web_admin as web_admin_service

    clean_body = body_text.strip()
    if not clean_body:
        return _detail_redirect(
            conversation_id,
            status="error",
            message="Reply body is required.",
        )
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )

    body_html = (
        "<p>" + "<br>".join(escape(line) for line in clean_body.splitlines()) + "</p>"
    )
    result = team_inbox_outbound.send_inbox_reply(
        db,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html=body_html,
            body_text=clean_body,
            sent_by_person_id=web_admin_service.get_actor_id(request),
            metadata={"source_route": "admin_inbox_detail_reply"},
        ),
    )
    if result.kind != "sent":
        return _detail_redirect(
            conversation_id,
            status="error",
            message=result.reason or "Reply could not be sent.",
        )

    team_inbox_operations.record_macro_use(db, macro_id)
    db.commit()
    sender = result.from_address or result.sender_key or "team sender"
    return _detail_redirect(
        conversation_id,
        status="success",
        message=f"Reply sent from {sender}.",
    )


@router.post(
    "/{conversation_id}/labels/create",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_label_create(
    conversation_id: UUID,
    name: str = Form(...),
    color: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        team_inbox_operations.create_or_reactivate_label(db, name=name, color=color)
    except team_inbox_operations.InboxOperationError as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    db.commit()
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Label saved.",
    )


@router.post(
    "/{conversation_id}/labels/apply",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_label_apply(
    conversation_id: UUID,
    request: Request,
    label_id: str = Form(...),
    db: Session = Depends(get_db),
):
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    try:
        team_inbox_operations.apply_label(
            db,
            conversation=conversation,
            label_id=label_id,
            applied_by_person_id=_actor_id_from_request(request),
        )
    except team_inbox_operations.InboxOperationError as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    db.commit()
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Label applied.",
    )


@router.post(
    "/{conversation_id}/labels/remove",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_label_remove(
    conversation_id: UUID,
    label_id: str = Form(...),
    db: Session = Depends(get_db),
):
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    team_inbox_operations.remove_label(db, conversation=conversation, label_id=label_id)
    db.commit()
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Label removed.",
    )


@router.post(
    "/{conversation_id}/macros/create",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_macro_create(
    conversation_id: UUID,
    request: Request,
    name: str = Form(...),
    body_text: str = Form(...),
    description: str | None = Form(default=None),
    visibility: str = Form(default="shared"),
    db: Session = Depends(get_db),
):
    try:
        team_inbox_operations.create_macro(
            db,
            name=name,
            body_text=body_text,
            description=description,
            visibility=visibility,
            created_by_person_id=_actor_id_from_request(request),
        )
    except team_inbox_operations.InboxOperationError as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    db.commit()
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Reply macro saved.",
    )


@router.post(
    "/{conversation_id}/contact-link",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_contact_link(
    conversation_id: UUID,
    request: Request,
    target_type: str = Form(...),
    subscriber_id: str | None = Form(default=None),
    reseller_id: str | None = Form(default=None),
    subscriber_id_manual: str | None = Form(default=None),
    reseller_id_manual: str | None = Form(default=None),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    from app.services import web_admin as web_admin_service

    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    selected_subscriber = (subscriber_id_manual or subscriber_id or "").strip() or None
    selected_reseller = (reseller_id_manual or reseller_id or "").strip() or None
    if target_type == "subscriber":
        selected_reseller = None
    elif target_type == "reseller":
        selected_subscriber = None
    else:
        return _detail_redirect(
            conversation_id,
            status="error",
            message="Choose whether this contact belongs to a subscriber or reseller.",
        )
    try:
        result = team_inbox_contact_links.link_conversation_contact(
            db,
            conversation=conversation,
            subscriber_id=selected_subscriber,
            reseller_id=selected_reseller,
            linked_by_person_id=web_admin_service.get_actor_id(request),
            note=note,
        )
    except team_inbox_contact_links.ContactLinkError as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    db.commit()
    target = "subscriber" if result.subscriber_id else "reseller"
    return _detail_redirect(
        conversation_id,
        status="success",
        message=f"Linked {conversation.channel_type.replace('_', ' ')} contact to {target}.",
    )


@router.post(
    "/{conversation_id}/note",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_internal_note(
    conversation_id: UUID,
    request: Request,
    body_text: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.services import web_admin as web_admin_service

    clean_body = body_text.strip()
    if not clean_body:
        return _detail_redirect(
            conversation_id,
            status="error",
            message="Internal note is required.",
        )
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    note = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction=InboxMessageDirection.internal.value,
        body=clean_body,
        from_address="internal",
        metadata_={
            "source": "admin_inbox_internal_note",
            "actor_id": web_admin_service.get_actor_id(request),
        },
    )
    db.add(note)
    db.commit()
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Internal note saved.",
    )


@router.post(
    "/{conversation_id}/status",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_status_action(
    conversation_id: UUID,
    request: Request,
    status_value: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.services import web_admin as web_admin_service

    allowed_statuses = {item.value for item in InboxConversationStatus}
    clean_status = status_value.strip().lower()
    if clean_status not in allowed_statuses:
        return _detail_redirect(
            conversation_id,
            status="error",
            message="Unsupported conversation status.",
        )
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    previous_status = conversation.status
    if previous_status == clean_status:
        return _detail_redirect(
            conversation_id,
            status="success",
            message=f"Conversation is already {clean_status}.",
        )
    metadata = dict(conversation.metadata_ or {})
    history = metadata.get("status_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "from": previous_status,
            "to": clean_status,
            "at": datetime.now(UTC).isoformat(),
            "actor_id": web_admin_service.get_actor_id(request),
            "source": "admin_inbox_status_action",
        }
    )
    metadata["status_history"] = history[-50:]
    conversation.status = clean_status
    conversation.metadata_ = metadata
    db.commit()
    return _detail_redirect(
        conversation_id,
        status="success",
        message=f"Conversation marked {clean_status.replace('_', ' ')}.",
    )
