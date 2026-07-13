"""Admin team inbox routes."""

from __future__ import annotations

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversationStatus,
)
from app.services import (
    team_inbox_commands,
    team_inbox_contact_links,
    team_inbox_metrics,
    team_inbox_operations,
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
    contact_resolution_status: str | None = Query(default=None),
    priority_at_most: int | None = Query(default=None, ge=0, le=999),
    muted: bool | None = Query(default=None),
    snoozed: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    clean_contact_resolution_status = (
        contact_resolution_status.strip()
        if isinstance(contact_resolution_status, str)
        and contact_resolution_status.strip()
        else None
    )
    clean_priority_at_most = (
        priority_at_most if isinstance(priority_at_most, int) else None
    )
    clean_muted = muted if isinstance(muted, bool) else None
    clean_snoozed = snoozed if isinstance(snoozed, bool) else None
    offset = (page - 1) * per_page
    result = team_inbox_read.list_conversations(
        db,
        search=search,
        status=status,
        channel_type=channel_type,
        service_team_id=service_team_id,
        assigned_person_id=assigned_person_id,
        needs_response=needs_response,
        contact_resolution_status=clean_contact_resolution_status,
        priority_at_most=clean_priority_at_most,
        muted=clean_muted,
        snoozed=clean_snoozed,
        limit=per_page,
        offset=offset,
    )
    context = _ctx(request, db)
    context.update(
        {
            "rows": result.items,
            "queue_metrics": team_inbox_operations.queue_metrics(db),
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
            "contact_resolution_status": clean_contact_resolution_status or "",
            "priority_at_most": clean_priority_at_most,
            "muted": clean_muted,
            "snoozed": clean_snoozed,
            "service_team_options": team_inbox_metrics.active_service_team_options(db),
            "status_options": [item.value for item in InboxConversationStatus],
            "channel_options": [item.value for item in InboxChannelType],
            "label_options": team_inbox_operations.list_labels(db),
            "saved_filters": team_inbox_operations.list_saved_filters(
                db, person_id=_actor_id_from_request(request)
            ),
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


def _contact_link_candidates(
    db: Session,
    timeline: team_inbox_read.InboxConversationTimeline,
) -> dict[str, list[dict[str, str]]]:
    return team_inbox_contact_links.contact_link_candidates(
        db,
        _candidate_terms(timeline),
    )


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
            "template_options": team_inbox_operations.list_templates(
                db, channel_type=timeline.channel_type
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
    body_text: str = Form(default=""),
    macro_id: str | None = Form(default=None),
    template_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        outcome = team_inbox_commands.reply(
            db,
            conversation_id=conversation_id,
            body_text=body_text,
            macro_id=macro_id,
            template_id=template_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=str(exc),
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message=(
            f"Reply queued from {outcome.sender}."
            if outcome.kind == "queued"
            else f"Reply sent from {outcome.sender}."
        ),
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
        team_inbox_commands.create_label(db, name=name, color=color)
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
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
    try:
        team_inbox_commands.apply_label(
            db,
            conversation_id=conversation_id,
            label_id=label_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
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
    try:
        team_inbox_commands.remove_label(
            db,
            conversation_id=conversation_id,
            label_id=label_id,
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
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
        team_inbox_commands.create_macro(
            db,
            name=name,
            body_text=body_text,
            description=description,
            visibility=visibility,
            actor_person_id=_actor_id_from_request(request),
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Reply macro saved.",
    )


@router.post(
    "/{conversation_id}/templates/create",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_template_create(
    conversation_id: UUID,
    name: str = Form(...),
    channel_type: str = Form(default="any"),
    subject: str | None = Form(default=None),
    body_text: str = Form(...),
    provider_template_name: str | None = Form(default=None),
    provider_template_language: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        team_inbox_commands.create_template(
            db,
            name=name,
            channel_type=channel_type,
            subject=subject,
            body_text=body_text,
            provider_template_name=provider_template_name,
            provider_template_language=provider_template_language,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Message template saved.",
    )


@router.post(
    "/messages/{message_id}/retry",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_message_retry(
    message_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        conversation_id = team_inbox_commands.retry_message(
            db,
            message_id=message_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.MessageNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Message%20not%20found",
            status_code=303,
        )
    except team_inbox_commands.InboxCommandRejected as exc:
        return _detail_redirect(
            exc.conversation_id or "",
            status="error",
            message=str(exc),
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Message queued for retry.",
    )


@router.post(
    "/messages/retry-failed",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_retry_failed_batch(
    db: Session = Depends(get_db),
):
    retry_count = team_inbox_commands.retry_failed_batch(db, limit=50)
    return RedirectResponse(
        url=(
            "/admin/inbox/reports/outbox-failures"
            f"?status=success&message={quote_plus(f'Retried {retry_count} failed messages.')}"
        ),
        status_code=303,
    )


@router.post(
    "/{conversation_id}/workflow",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_workflow_action(
    conversation_id: UUID,
    request: Request,
    priority: int | None = Form(default=None),
    is_muted: bool | None = Form(default=None),
    snooze_minutes: int | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        team_inbox_commands.update_workflow(
            db,
            conversation_id=conversation_id,
            priority=priority,
            is_muted=is_muted,
            snooze_minutes=snooze_minutes,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Conversation workflow updated.",
    )


@router.post(
    "/filters/save",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_saved_filter_create(
    request: Request,
    name: str = Form(...),
    search: str | None = Form(default=None),
    status_value: str | None = Form(default=None),
    channel_type: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    needs_response: bool = Form(default=False),
    contact_resolution_status: str | None = Form(default=None),
    priority_at_most: int | None = Form(default=None),
    muted: bool | None = Form(default=None),
    snoozed: bool | None = Form(default=None),
    is_shared: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    try:
        team_inbox_commands.save_filter(
            db,
            name=name,
            filter_payload={
                "search": search,
                "status": status_value,
                "channel_type": channel_type,
                "service_team_id": service_team_id,
                "needs_response": needs_response,
                "contact_resolution_status": contact_resolution_status,
                "priority_at_most": priority_at_most,
                "muted": muted,
                "snoozed": snoozed,
            },
            actor_person_id=_actor_id_from_request(request),
            is_shared=is_shared,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/inbox?status=success&message=Saved%20filter%20created",
        status_code=303,
    )


@router.get(
    "/reports/outbox-failures",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_outbox_failures(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    context.update(
        {
            "messages": team_inbox_operations.list_failed_outbound_messages(db),
        }
    )
    return templates.TemplateResponse("admin/inbox/outbox_failures.html", context)


@router.post(
    "/bulk",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_bulk_action(
    request: Request,
    conversation_ids: list[str] = Form(default=[]),
    action: str = Form(...),
    status_value: str | None = Form(default=None),
    label_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    assigned_person_id: str | None = Form(default=None),
    auto_assign: bool = Form(default=True),
    db: Session = Depends(get_db),
):
    try:
        outcome = team_inbox_commands.bulk_action(
            db,
            conversation_ids=conversation_ids,
            action=action,
            status_value=status_value,
            label_id=label_id,
            service_team_id=service_team_id,
            assigned_person_id=assigned_person_id,
            auto_assign=auto_assign,
            actor_person_id=_actor_id_from_request(request),
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/inbox?status=success&message={quote_plus(outcome.message)}",
        status_code=303,
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
    try:
        outcome = team_inbox_commands.link_contact(
            db,
            conversation_id=conversation_id,
            target_type=target_type,
            subscriber_id=subscriber_id,
            reseller_id=reseller_id,
            subscriber_id_manual=subscriber_id_manual,
            reseller_id_manual=reseller_id_manual,
            actor_person_id=_actor_id_from_request(request),
            note=note,
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_contact_links.ContactLinkError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message=(
            f"Linked {outcome.channel_type.replace('_', ' ')} contact to "
            f"{outcome.target}."
        ),
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
    try:
        team_inbox_commands.create_internal_note(
            db,
            conversation_id=conversation_id,
            body=body_text,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Internal note saved.",
    )


@router.post(
    "/{conversation_id}/comments",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_comment_create(
    conversation_id: UUID,
    request: Request,
    body_text: str = Form(...),
    message_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        team_inbox_commands.create_comment(
            db,
            conversation_id=conversation_id,
            body=body_text,
            message_id=message_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Comment saved.",
    )


@router.post(
    "/comments/{comment_id}/resolve",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_comment_resolve(
    comment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        conversation_id = team_inbox_commands.resolve_comment(
            db,
            comment_id=comment_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Comment resolved.",
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
    try:
        outcome = team_inbox_commands.update_status(
            db,
            conversation_id=conversation_id,
            status_value=status_value,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except team_inbox_commands.InboxCommandError as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=str(exc),
        )
    if outcome.already_set:
        return _detail_redirect(
            conversation_id,
            status="success",
            message=f"Conversation is already {outcome.status}.",
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message=f"Conversation marked {outcome.status.replace('_', ' ')}.",
    )
