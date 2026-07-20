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
    subscriber as subscriber_service,
)
from app.services import (
    team_inbox_commands,
    team_inbox_contact_links,
    team_inbox_metrics,
    team_inbox_operations,
    team_inbox_read,
)
from app.services.auth_dependencies import require_permission
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    PageMeta,
    request_needs_canonicalization,
)

router = APIRouter(prefix="/inbox", tags=["web-admin-inbox"])
templates = Jinja2Templates(directory="templates")

# The inbox queue's declared capabilities. The default sort is "priority", which
# list_conversations maps to the urgency composite (priority, then recency), so
# the default view is unchanged; last_message_at / created_at are single-column
# sorts. All filters are declared so list_query.url round-trips them on a
# sort/page click.
INBOX_LIST_DEFINITION = ListDefinition(
    key="team_inbox",
    fields=(
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("channel_type", "Channel", filterable=True),
        ListFieldDefinition("service_team_id", "Team", filterable=True),
        ListFieldDefinition("assigned_person_id", "Assignee", filterable=True),
        ListFieldDefinition("contact_resolution_status", "Contact", filterable=True),
        ListFieldDefinition("needs_response", "Needs response", filterable=True),
        ListFieldDefinition("muted", "Muted", filterable=True),
        ListFieldDefinition("snoozed", "Snoozed", filterable=True),
        ListFieldDefinition("open_only", "Open only", filterable=True),
        ListFieldDefinition("unassigned", "Unassigned", filterable=True),
        ListFieldDefinition("priority_at_most", "Max priority", filterable=True),
        ListFieldDefinition("priority", "Priority", sortable=True),
        ListFieldDefinition("last_message_at", "Last activity", sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="priority",
    default_sort_dir="asc",
    per_page_options=(10, 25, 50, 100),
    default_per_page=25,
)


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "team-inbox",
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _clean_uuid(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        return str(UUID(candidate))
    except (TypeError, ValueError, AttributeError):
        return None


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
    priority_at_most: int | None = Query(default=None),
    muted: bool | None = Query(default=None),
    snoozed: bool | None = Query(default=None),
    open_only: bool = Query(default=False),
    unassigned: bool = Query(default=False),
    sort_by: str | None = Query(default=None, alias="sort"),
    sort_dir: str | None = Query(default=None, alias="dir"),
    page: int = Query(default=1),
    per_page: int = Query(default=25),
    c: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    # FastAPI resolves Query defaults before normal route execution, but focused
    # route tests and internal adapters can call this function directly. In that
    # case omitted values are still ``Query`` objects; normalize them at the
    # adapter boundary so they cannot look truthy or trigger a false redirect.
    search = search if isinstance(search, str) else None
    status = status if isinstance(status, str) else None
    channel_type = channel_type if isinstance(channel_type, str) else None
    service_team_id = service_team_id if isinstance(service_team_id, str) else None
    assigned_person_id = (
        assigned_person_id if isinstance(assigned_person_id, str) else None
    )
    needs_response = needs_response if isinstance(needs_response, bool) else False
    contact_resolution_status = (
        contact_resolution_status
        if isinstance(contact_resolution_status, str)
        else None
    )
    priority_at_most = (
        priority_at_most
        if isinstance(priority_at_most, int) and not isinstance(priority_at_most, bool)
        else None
    )
    muted = muted if isinstance(muted, bool) else None
    snoozed = snoozed if isinstance(snoozed, bool) else None
    open_only = open_only if isinstance(open_only, bool) else False
    unassigned = unassigned if isinstance(unassigned, bool) else False
    sort_by = sort_by if isinstance(sort_by, str) else None
    sort_dir = sort_dir if isinstance(sort_dir, str) else None
    page = page if isinstance(page, int) and not isinstance(page, bool) else 1
    per_page = (
        per_page if isinstance(per_page, int) and not isinstance(per_page, bool) else 25
    )
    c = c if isinstance(c, str) else None
    requested_status = status
    requested_channel_type = channel_type
    requested_service_team_id = service_team_id
    requested_assigned_person_id = assigned_person_id
    requested_priority_at_most = priority_at_most
    status_values = {item.value for item in InboxConversationStatus}
    channel_values = {item.value for item in InboxChannelType}
    status = status if status in status_values else None
    channel_type = channel_type if channel_type in channel_values else None
    service_team_id = _clean_uuid(service_team_id)
    assigned_person_id = _clean_uuid(assigned_person_id)
    clean_contact_resolution_status = (
        contact_resolution_status.strip()
        if isinstance(contact_resolution_status, str)
        and contact_resolution_status.strip()
        else None
    )
    clean_priority_at_most = (
        priority_at_most
        if isinstance(priority_at_most, int) and 0 <= priority_at_most <= 999
        else None
    )
    clean_muted = muted if isinstance(muted, bool) else None
    clean_snoozed = snoozed if isinstance(snoozed, bool) else None
    clean_open_only = open_only if isinstance(open_only, bool) else False
    clean_unassigned = unassigned if isinstance(unassigned, bool) else False
    definition = INBOX_LIST_DEFINITION
    safe_sort = (
        sort_by if sort_by in definition.sortable_keys else definition.default_sort
    )
    safe_dir = sort_dir if sort_dir in ("asc", "desc") else None
    safe_per_page = (
        per_page
        if per_page in definition.per_page_options
        else definition.default_per_page
    )
    requested_query = definition.build_query(
        search=search,
        filters={
            "status": status,
            "channel_type": channel_type,
            "service_team_id": service_team_id,
            "assigned_person_id": assigned_person_id,
            "contact_resolution_status": clean_contact_resolution_status,
            "needs_response": "true" if needs_response else None,
            "muted": ("true" if clean_muted else "false")
            if clean_muted is not None
            else None,
            "snoozed": ("true" if clean_snoozed else "false")
            if clean_snoozed is not None
            else None,
            "open_only": "true" if clean_open_only else None,
            "unassigned": "true" if clean_unassigned else None,
            "priority_at_most": str(clean_priority_at_most)
            if clean_priority_at_most is not None
            else None,
        },
        sort_by=safe_sort,
        sort_dir=safe_dir,
        page=max(1, page),
        per_page=safe_per_page,
    )
    result = team_inbox_read.list_conversations(
        db,
        search=requested_query.search,
        status=status,
        channel_type=channel_type,
        service_team_id=service_team_id,
        assigned_person_id=assigned_person_id,
        needs_response=needs_response,
        contact_resolution_status=clean_contact_resolution_status,
        priority_at_most=clean_priority_at_most,
        muted=clean_muted,
        snoozed=clean_snoozed,
        open_only=clean_open_only,
        unassigned=clean_unassigned,
        order_by=requested_query.sort_by,
        order_dir=requested_query.sort_dir,
        limit=requested_query.per_page,
        offset=requested_query.offset,
    )
    page_meta = PageMeta.from_query(requested_query, result.count)
    list_query = requested_query.with_page(page_meta.page)
    if list_query.page != requested_query.page:
        result = team_inbox_read.list_conversations(
            db,
            search=list_query.search,
            status=status,
            channel_type=channel_type,
            service_team_id=service_team_id,
            assigned_person_id=assigned_person_id,
            needs_response=needs_response,
            contact_resolution_status=clean_contact_resolution_status,
            priority_at_most=clean_priority_at_most,
            muted=clean_muted,
            snoozed=clean_snoozed,
            open_only=clean_open_only,
            unassigned=clean_unassigned,
            order_by=list_query.sort_by,
            order_dir=list_query.sort_dir,
            limit=list_query.per_page,
            offset=list_query.offset,
        )
    raw_filters = {
        "status": requested_status,
        "channel_type": requested_channel_type,
        "service_team_id": requested_service_team_id,
        "assigned_person_id": requested_assigned_person_id,
        "contact_resolution_status": contact_resolution_status,
        "needs_response": "true" if needs_response else None,
        "muted": ("true" if muted else "false") if muted is not None else None,
        "snoozed": ("true" if snoozed else "false") if snoozed is not None else None,
        "open_only": "true" if open_only else None,
        "unassigned": "true" if unassigned else None,
        "priority_at_most": str(requested_priority_at_most)
        if requested_priority_at_most is not None
        else None,
    }
    if request_needs_canonicalization(
        list_query,
        search=search,
        filters=raw_filters,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    ):
        return RedirectResponse(url=list_query.url("/admin/inbox"), status_code=307)
    context = _ctx(request, db)
    context.update(
        {
            "rows": result.items,
            "queue_metrics": team_inbox_operations.queue_metrics(db),
            "count": result.count,
            "list_query": list_query,
            "page_meta": page_meta,
            "page": page_meta.page,
            "per_page": page_meta.per_page,
            "has_previous": page_meta.has_previous,
            "has_next": page_meta.has_next,
            "search": list_query.search or "",
            "status": status or "",
            "channel_type": channel_type or "",
            "service_team_id": service_team_id or "",
            "assigned_person_id": assigned_person_id or "",
            "needs_response": needs_response,
            "contact_resolution_status": clean_contact_resolution_status or "",
            "priority_at_most": clean_priority_at_most,
            "muted": clean_muted,
            "snoozed": clean_snoozed,
            "open_only": clean_open_only,
            "unassigned": clean_unassigned,
            "service_team_options": team_inbox_metrics.active_service_team_options(db),
            "status_options": [item.value for item in InboxConversationStatus],
            "channel_options": [item.value for item in InboxChannelType],
            "label_options": team_inbox_operations.list_labels(db),
            "saved_filters": team_inbox_operations.list_saved_filters(
                db, person_id=_actor_id_from_request(request)
            ),
        }
    )
    selected_view = None
    if c:
        try:
            selected_view = _conversation_view(db, request, UUID(c))
        except (ValueError, TypeError):
            selected_view = None
    if selected_view is not None:
        context["selected"] = selected_view["timeline"]
        context.update(selected_view)
    else:
        context["selected"] = None
    return templates.TemplateResponse("admin/inbox/index.html", context)


def _detail_redirect(
    conversation_id: str | UUID,
    *,
    status: str,
    message: str,
) -> RedirectResponse:
    return RedirectResponse(
        url=(
            f"/admin/inbox?c={conversation_id}&status={quote_plus(status)}"
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


def _subscriber_summary(db: Session, subscriber_id: str | None) -> dict | None:
    """Minimal server-owned identity projection for the inbox context rail.

    Plan/balance/network are intentionally out of scope here; they belong to the
    subscriber-360 projection (docs/design/ADMIN_UI_ARCHETYPES.md). A bad or
    stale id must never break the thread view.
    """
    if not subscriber_id:
        return None
    try:
        subscriber = subscriber_service.subscribers.get(
            db=db, subscriber_id=subscriber_id
        )
    except Exception:  # noqa: BLE001
        return None
    if subscriber is None:
        return None
    status = getattr(subscriber, "status", None)
    kind = "business" if getattr(subscriber, "is_business", False) else "person"
    name = (
        getattr(subscriber, "display_name", None)
        or getattr(subscriber, "company_name", None)
        or " ".join(
            part
            for part in (
                getattr(subscriber, "first_name", None),
                getattr(subscriber, "last_name", None),
            )
            if part
        )
        or getattr(subscriber, "email", None)
    )
    return {
        "id": str(subscriber_id),
        "name": name,
        "account_number": getattr(subscriber, "account_number", None),
        "subscriber_number": getattr(subscriber, "subscriber_number", None),
        "status": status.value
        if hasattr(status, "value")
        else (str(status) if status else None),
        "email": getattr(subscriber, "email", None),
        "phone": getattr(subscriber, "phone", None),
        "is_business": kind == "business",
        "url": f"/admin/customers/{kind}/{subscriber_id}",
    }


def _conversation_view(
    db: Session, request: Request, conversation_id: UUID
) -> dict | None:
    """Full conversation projection shared by the workspace page and the HTMX
    detail partial. Returns None when the conversation does not exist."""
    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        return None
    return {
        "timeline": timeline,
        "subscriber_summary": _subscriber_summary(db, timeline.subscriber_id),
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
    view = _conversation_view(db, request, conversation_id)
    if view is None:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    # HTMX list clicks swap the thread+context partial into #triage-detail;
    # a full navigation lands in the workspace with the conversation preselected.
    if request.headers.get("hx-request"):
        context = _ctx(request, db)
        context.update(view)
        return templates.TemplateResponse("admin/inbox/_conversation.html", context)
    return RedirectResponse(url=f"/admin/inbox?c={conversation_id}", status_code=303)


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
    open_only: bool = Form(default=False),
    unassigned: bool = Form(default=False),
    is_shared: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    clean_open_only = open_only if isinstance(open_only, bool) else False
    clean_unassigned = unassigned if isinstance(unassigned, bool) else False
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
                "open_only": clean_open_only,
                "unassigned": clean_unassigned,
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
