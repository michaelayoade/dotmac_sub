"""Admin support tickets routes."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import Subscriber
from app.models.support import (
    Ticket,
    TicketChannel,
    TicketPriority,
    TicketStatus,
)
from app.schemas.support import (
    AttachmentMeta,
    TicketCommentCreate,
    TicketCreate,
    TicketLinkCreate,
    TicketMergeRequest,
    TicketUpdate,
)
from app.services import support as support_service
from app.services.auth_dependencies import require_permission
from app.services.file_storage import file_uploads

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/support/tickets", tags=["web-admin-support-tickets"])
templates = Jinja2Templates(directory="templates")

_ALLOWED_ATTACHMENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
_DEFAULT_VISIBLE_COLUMNS = [
    "number",
    "ticket_type",
    "priority",
    "status",
    "customer",
    "created_at",
]
_TICKET_COLUMNS = [
    {"key": "number", "label": "Ticket ID"},
    {"key": "ticket_type", "label": "Ticket Type"},
    {"key": "priority", "label": "Priority"},
    {"key": "status", "label": "Status"},
    {"key": "customer", "label": "Customer Name"},
    {"key": "customer_id", "label": "Customer ID"},
    {"key": "subscriber", "label": "Subscriber"},
    {"key": "region", "label": "Region"},
    {"key": "technician", "label": "Assigned Technician"},
    {"key": "project_manager", "label": "Project Manager"},
    {"key": "site_coordinator", "label": "Site Coordinator"},
    {"key": "channel", "label": "Channel"},
    {"key": "due_at", "label": "Due Date"},
    {"key": "created_at", "label": "Opening Date"},
]


def _ctx(request: Request, db: Session, active_page: str = "support-tickets") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    value = current_user.get("subscriber_id") if current_user else None
    return str(value) if value else None


def _parse_uuid_or_none(value: str | None) -> UUID | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


def _parse_dt_or_none(value: str | None):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _subscriber_label(row: Subscriber) -> str:
    full_name = f"{row.first_name or ''} {row.last_name or ''}".strip()
    return row.display_name or full_name or row.email or str(row.id)


def _service_team_options() -> list[dict[str, str]]:
    return [
        {"id": "8e4f0b90-2de0-4d8c-8af1-c3f3a5f6ca01", "label": "Field Operations"},
        {"id": "3ac5eb8c-bdcf-4d03-9c8c-623ee7f8898e", "label": "Core Network"},
        {"id": "df39d87d-d31e-4dc8-9968-6fd95d7bb67f", "label": "Customer Support"},
    ]


def _upload_ticket_attachments(
    db: Session,
    *,
    request: Request,
    ticket_id: str,
    attachments: list[UploadFile],
    entity_type: str,
) -> list[dict]:
    uploaded_records = []
    uploaded_metadata = []
    actor_id = _actor_id(request)
    try:
        for attachment in attachments or []:
            filename = (attachment.filename or "").strip()
            if not filename:
                continue
            payload = attachment.file.read()
            if not payload:
                continue
            if len(payload) > _MAX_ATTACHMENT_BYTES:
                raise ValueError(f"{filename}: max file size is 5 MB")
            content_type = (
                attachment.content_type or "application/octet-stream"
            ).lower()
            if content_type not in _ALLOWED_ATTACHMENT_TYPES:
                raise ValueError(f"{filename}: unsupported file type")

            record = file_uploads.upload(
                db=db,
                domain="attachments",
                entity_type=entity_type,
                entity_id=ticket_id,
                original_filename=filename,
                content_type=content_type,
                data=payload,
                uploaded_by=actor_id,
                owner_subscriber_id=None,
            )
            uploaded_records.append(record)
            uploaded_metadata.append(
                {
                    "file_name": record.original_filename,
                    "content_type": record.content_type or content_type,
                    "file_size": int(record.file_size),
                    "storage_key": record.storage_key_or_relative_path,
                    "stored_file_id": str(record.id),
                }
            )
        return uploaded_metadata
    except Exception:
        for record in uploaded_records:
            try:
                file_uploads.soft_delete(db=db, file=record, hard_delete_object=True)
            except Exception:
                logger.warning(
                    "Failed to clean up uploaded support ticket attachment %s",
                    getattr(record, "id", None),
                    exc_info=True,
                )
        raise


def _build_form_context(
    request: Request, db: Session, *, ticket: Ticket | None = None
) -> dict:
    people = support_service.list_people(db)
    teams = _service_team_options()

    current_assignees: list[str] = []
    if ticket and ticket.assignees:
        current_assignees = [
            str(row.person_id) for row in ticket.assignees if row.person_id
        ]

    prefill = {
        "title": ticket.title if ticket else request.query_params.get("title", ""),
        "description": ticket.description
        if ticket
        else request.query_params.get("description", ""),
        "subscriber_id": str(ticket.subscriber_id)
        if ticket and ticket.subscriber_id
        else request.query_params.get("subscriber_id", ""),
        "customer_account_id": str(ticket.customer_account_id)
        if ticket and ticket.customer_account_id
        else request.query_params.get("customer_account_id", ""),
        "customer_person_id": str(ticket.customer_person_id)
        if ticket and ticket.customer_person_id
        else request.query_params.get("customer_person_id", ""),
        "region": ticket.region if ticket else request.query_params.get("region", ""),
        "ticket_type": ticket.ticket_type
        if ticket
        else request.query_params.get("ticket_type", ""),
        "priority": ticket.priority.value
        if ticket
        else request.query_params.get("priority", TicketPriority.normal.value),
        "channel": ticket.channel.value
        if ticket
        else request.query_params.get("channel", TicketChannel.web.value),
        "status": ticket.status.value
        if ticket
        else request.query_params.get("status", TicketStatus.open.value),
        "due_at": ticket.due_at.strftime("%Y-%m-%dT%H:%M")
        if ticket and ticket.due_at
        else "",
        "tags": ",".join(ticket.tags or [])
        if ticket
        else request.query_params.get("tags", ""),
        "related_outage_ticket_id": request.query_params.get(
            "related_outage_ticket_id", ""
        ),
        "technician_person_id": str(ticket.technician_person_id)
        if ticket and ticket.technician_person_id
        else "",
        "ticket_manager_person_id": str(ticket.ticket_manager_person_id)
        if ticket and ticket.ticket_manager_person_id
        else "",
        "site_coordinator_person_id": str(ticket.site_coordinator_person_id)
        if ticket and ticket.site_coordinator_person_id
        else "",
        "service_team_id": str(ticket.service_team_id)
        if ticket and ticket.service_team_id
        else "",
        "assignee_person_ids": current_assignees,
    }

    return {
        "all_statuses": [item.value for item in TicketStatus],
        "all_priorities": [item.value for item in TicketPriority],
        "all_channels": [item.value for item in TicketChannel],
        "region_options": support_service.regions(db),
        "ticket_type_options": support_service.ticket_types(db),
        "service_team_options": teams,
        "people_options": people,
        "prefill": prefill,
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def tickets_list(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    assigned_to_me: bool = Query(default=False),
    project_manager_person_id: str | None = Query(default=None),
    site_coordinator_person_id: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page
    actor_id = _actor_id(request) if assigned_to_me else None
    tickets = support_service.tickets.list(
        db,
        search=search,
        status=status,
        ticket_type=ticket_type,
        assigned_to_person_id=actor_id,
        project_manager_person_id=project_manager_person_id,
        site_coordinator_person_id=site_coordinator_person_id,
        subscriber_id=subscriber_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=per_page,
        offset=offset,
    )

    raw_columns = request.cookies.get(
        "ticket_columns", ",".join(_DEFAULT_VISIBLE_COLUMNS)
    ).split(",")
    visible_columns = [
        col
        for col in raw_columns
        if any(col == item["key"] for item in _TICKET_COLUMNS)
    ]
    if not visible_columns:
        visible_columns = list(_DEFAULT_VISIBLE_COLUMNS)

    context = _ctx(request, db)
    people_lookup = {
        item["id"]: item["label"] for item in support_service.list_people(db)
    }
    context.update(
        {
            "tickets": tickets,
            "search": search or "",
            "status": status or "",
            "ticket_type": ticket_type or "",
            "assigned_to_me": assigned_to_me,
            "project_manager_person_id": project_manager_person_id or "",
            "site_coordinator_person_id": site_coordinator_person_id or "",
            "subscriber_id": subscriber_id or "",
            "order_by": order_by,
            "order_dir": order_dir,
            "page": page,
            "per_page": per_page,
            "has_next_page": len(tickets) >= per_page,
            "status_totals": support_service.status_totals(db),
            "visible_columns": visible_columns,
            "ticket_columns": _TICKET_COLUMNS,
            "all_statuses": [item.value for item in TicketStatus],
            "all_priorities": [item.value for item in TicketPriority],
            "ticket_type_options": support_service.ticket_types(db),
            "people_options": support_service.list_people(db),
            "people_lookup": people_lookup,
        }
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("admin/support/tickets/_table.html", context)
    return templates.TemplateResponse("admin/support/tickets/index.html", context)


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def ticket_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update(_build_form_context(request, db))
    context.update({"page_title": "New Ticket", "form_mode": "create", "ticket": None})
    return templates.TemplateResponse("admin/support/tickets/new.html", context)


@router.get(
    "/{ticket_lookup}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_edit_page(
    request: Request, ticket_lookup: str, db: Session = Depends(get_db)
):
    ticket = support_service.tickets.get_by_lookup(db, ticket_lookup)
    context = _ctx(request, db)
    context.update(_build_form_context(request, db, ticket=ticket))
    context.update({"page_title": "Edit Ticket", "form_mode": "edit", "ticket": ticket})
    return templates.TemplateResponse("admin/support/tickets/new.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def ticket_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    subscriber_id: str | None = Form(default=None),
    customer_account_id: str | None = Form(default=None),
    customer_person_id: str | None = Form(default=None),
    region: str | None = Form(default=None),
    technician_person_id: str | None = Form(default=None),
    ticket_manager_person_id: str | None = Form(default=None),
    site_coordinator_person_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    ticket_type: str | None = Form(default=None),
    priority: str = Form("normal"),
    channel: str = Form("web"),
    status: str = Form("open"),
    due_at: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    related_outage_ticket_id: str | None = Form(default=None),
    assignee_person_ids: list[str] = Form(default=[]),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id(request)

    tag_list = [item.strip() for item in (tags or "").split(",") if item.strip()]
    payload = TicketCreate(
        title=title,
        description=description or None,
        subscriber_id=_parse_uuid_or_none(subscriber_id),
        customer_account_id=_parse_uuid_or_none(customer_account_id),
        customer_person_id=_parse_uuid_or_none(customer_person_id),
        created_by_person_id=_parse_uuid_or_none(actor_id),
        region=region or None,
        technician_person_id=_parse_uuid_or_none(technician_person_id),
        ticket_manager_person_id=_parse_uuid_or_none(ticket_manager_person_id),
        site_coordinator_person_id=_parse_uuid_or_none(site_coordinator_person_id),
        service_team_id=_parse_uuid_or_none(service_team_id),
        ticket_type=ticket_type or None,
        priority=priority,
        channel=channel,
        status=status,
        due_at=_parse_dt_or_none(due_at),
        tags=tag_list,
        assignee_person_ids=[
            uid
            for uid in (_parse_uuid_or_none(item) for item in assignee_person_ids)
            if uid
        ],
        related_outage_ticket_id=_parse_uuid_or_none(related_outage_ticket_id),
    )

    ticket = support_service.tickets.create(
        db, payload, actor_id=actor_id, request=request
    )
    if attachments:
        uploaded = _upload_ticket_attachments(
            db,
            request=request,
            ticket_id=str(ticket.id),
            attachments=attachments,
            entity_type="support_ticket_attachment",
        )
        support_service.tickets.add_attachments(db, str(ticket.id), uploaded)

    return RedirectResponse(url=f"/admin/support/tickets/{ticket.id}", status_code=303)


@router.get(
    "/{ticket_lookup}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def ticket_detail(request: Request, ticket_lookup: str, db: Session = Depends(get_db)):
    ticket = support_service.tickets.get_by_lookup(db, ticket_lookup)
    comments = support_service.ticket_comments.list(
        db, str(ticket.id), limit=500, offset=0
    )
    sla_events = support_service.ticket_sla_events.list(
        db, str(ticket.id), limit=200, offset=0
    )
    links = support_service.tickets.list_links(db, str(ticket.id), limit=100)

    from app.services.audit_helpers import build_audit_activities

    activities = build_audit_activities(db, "support_ticket", str(ticket.id), limit=100)

    context = _ctx(request, db)
    context.update(
        {
            "ticket": ticket,
            "comments": comments,
            "sla_events": sla_events,
            "ticket_links": links,
            "activities": activities,
            "all_statuses": [item.value for item in TicketStatus],
            "all_priorities": [item.value for item in TicketPriority],
            "all_channels": [item.value for item in TicketChannel],
            "people_options": support_service.list_people(db),
            "service_team_options": _service_team_options(),
            "is_merged_source": bool(
                ticket.merged_into_ticket_id or ticket.status.value == "merged"
            ),
        }
    )
    return templates.TemplateResponse("admin/support/tickets/detail.html", context)


@router.post(
    "/{ticket_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_edit(
    request: Request,
    ticket_id: UUID,
    title: str = Form(...),
    description: str = Form(""),
    subscriber_id: str | None = Form(default=None),
    customer_account_id: str | None = Form(default=None),
    customer_person_id: str | None = Form(default=None),
    region: str | None = Form(default=None),
    status: str = Form("open"),
    priority: str = Form("normal"),
    channel: str = Form("web"),
    ticket_type: str | None = Form(default=None),
    due_at: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    technician_person_id: str | None = Form(default=None),
    ticket_manager_person_id: str | None = Form(default=None),
    site_coordinator_person_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    assignee_person_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    payload = TicketUpdate(
        title=title,
        description=description or None,
        subscriber_id=_parse_uuid_or_none(subscriber_id),
        customer_account_id=_parse_uuid_or_none(customer_account_id),
        customer_person_id=_parse_uuid_or_none(customer_person_id),
        region=region or None,
        status=status,
        priority=priority,
        channel=channel,
        ticket_type=ticket_type or None,
        due_at=_parse_dt_or_none(due_at),
        tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
        technician_person_id=_parse_uuid_or_none(technician_person_id),
        ticket_manager_person_id=_parse_uuid_or_none(ticket_manager_person_id),
        site_coordinator_person_id=_parse_uuid_or_none(site_coordinator_person_id),
        service_team_id=_parse_uuid_or_none(service_team_id),
        assignee_person_ids=[
            uid
            for uid in (_parse_uuid_or_none(item) for item in assignee_person_ids)
            if uid
        ],
    )
    support_service.tickets.update(
        db, str(ticket_id), payload, actor_id=_actor_id(request), request=request
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/comment",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_add_comment(
    request: Request,
    ticket_id: UUID,
    body: str = Form(...),
    is_internal: bool = Form(False),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    uploaded = _upload_ticket_attachments(
        db,
        request=request,
        ticket_id=str(ticket_id),
        attachments=attachments,
        entity_type="support_ticket_comment_attachment",
    )
    payload = TicketCommentCreate(
        body=body,
        is_internal=is_internal,
        author_person_id=_parse_uuid_or_none(_actor_id(request)),
        attachments=[AttachmentMeta(**item) for item in uploaded],
    )
    support_service.tickets.create_comment(
        db, str(ticket_id), payload, actor_id=_actor_id(request), request=request
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/auto-assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_auto_assign(
    request: Request, ticket_id: UUID, db: Session = Depends(get_db)
):
    support_service.tickets.manual_auto_assign(
        db, str(ticket_id), actor_id=_actor_id(request), request=request
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/link",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_link(
    request: Request,
    ticket_id: UUID,
    to_ticket_id: str = Form(...),
    link_type: str = Form(...),
    db: Session = Depends(get_db),
):
    payload = TicketLinkCreate(to_ticket_id=UUID(to_ticket_id), link_type=link_type)
    support_service.tickets.link_ticket(
        db,
        from_ticket_id=str(ticket_id),
        to_ticket_id=str(payload.to_ticket_id),
        link_type=payload.link_type,
        actor_id=_actor_id(request),
        request=request,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{ticket_id}", status_code=303)


@router.post(
    "/{ticket_id}/merge",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def ticket_merge(
    request: Request,
    ticket_id: UUID,
    target_ticket_id: str = Form(...),
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    target = support_service.tickets.merge(
        db,
        str(ticket_id),
        TicketMergeRequest(target_ticket_id=UUID(target_ticket_id), reason=reason),
        actor_id=_actor_id(request),
        request=request,
    )
    return RedirectResponse(url=f"/admin/support/tickets/{target.id}", status_code=303)


@router.post(
    "/{ticket_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:delete"))],
)
def ticket_delete(request: Request, ticket_id: UUID, db: Session = Depends(get_db)):
    support_service.tickets.soft_delete(
        db, str(ticket_id), actor_id=_actor_id(request), request=request
    )
    if request.headers.get("HX-Request"):
        headers = {
            "HX-Redirect": "/admin/support/tickets",
            "HX-Trigger": json.dumps(
                {
                    "showToast": {
                        "type": "success",
                        "title": "Ticket deleted",
                        "message": "Ticket was archived.",
                    }
                }
            ),
        }
        return Response(status_code=204, headers=headers)
    return RedirectResponse(url="/admin/support/tickets", status_code=303)


# Legacy path compatibility used by existing smoke tests.
legacy_router = APIRouter(prefix="/tickets", tags=["web-admin-support-tickets-legacy"])


@legacy_router.get("", response_class=HTMLResponse)
def legacy_ticket_index():
    return RedirectResponse(url="/admin/support/tickets", status_code=307)


@legacy_router.get("/{ticket_lookup}", response_class=HTMLResponse)
def legacy_ticket_detail(ticket_lookup: str):
    return RedirectResponse(
        url=f"/admin/support/tickets/{ticket_lookup}", status_code=307
    )
