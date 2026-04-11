"""Web helpers for admin support ticket routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.support import Ticket, TicketChannel, TicketPriority, TicketStatus
from app.schemas.support import (
    AttachmentMeta,
    TicketCommentCreate,
    TicketCreate,
    TicketLinkCreate,
    TicketMergeRequest,
    TicketUpdate,
)
from app.services import support as support_service
from app.services.file_storage import file_uploads

logger = logging.getLogger(__name__)

ALLOWED_ATTACHMENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
DEFAULT_VISIBLE_COLUMNS = [
    "number",
    "ticket_type",
    "priority",
    "status",
    "customer",
    "created_at",
]
TICKET_COLUMNS = [
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


def parse_uuid_or_none(value: str | None) -> UUID | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


def parse_dt_or_none(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def service_team_options() -> list[dict[str, str]]:
    return [
        {"id": "8e4f0b90-2de0-4d8c-8af1-c3f3a5f6ca01", "label": "Field Operations"},
        {"id": "3ac5eb8c-bdcf-4d03-9c8c-623ee7f8898e", "label": "Core Network"},
        {"id": "df39d87d-d31e-4dc8-9968-6fd95d7bb67f", "label": "Customer Support"},
    ]


def visible_ticket_columns(raw_cookie: str | None) -> list[str]:
    raw_columns = (raw_cookie or ",".join(DEFAULT_VISIBLE_COLUMNS)).split(",")
    visible_columns = [
        column
        for column in raw_columns
        if any(column == item["key"] for item in TICKET_COLUMNS)
    ]
    return visible_columns or list(DEFAULT_VISIBLE_COLUMNS)


def upload_ticket_attachments(
    db: Session,
    *,
    ticket_id: str,
    attachments: list,
    entity_type: str,
    actor_id: str | None,
) -> list[dict]:
    uploaded_records = []
    uploaded_metadata = []
    try:
        for attachment in attachments or []:
            filename = (getattr(attachment, "filename", "") or "").strip()
            if not filename:
                continue
            payload = attachment.file.read()
            if not payload:
                continue
            if len(payload) > MAX_ATTACHMENT_BYTES:
                raise ValueError(f"{filename}: max file size is 5 MB")
            content_type = (
                getattr(attachment, "content_type", None) or "application/octet-stream"
            ).lower()
            if content_type not in ALLOWED_ATTACHMENT_TYPES:
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


def build_ticket_form_context(
    db: Session,
    *,
    query_params: Mapping[str, object] | None = None,
    ticket: Ticket | None = None,
) -> dict:
    params = query_params or {}
    people = support_service.list_people(db)
    current_assignees = [
        str(row.person_id)
        for row in (ticket.assignees if ticket and ticket.assignees else [])
        if row.person_id
    ]
    prefill = {
        "title": ticket.title if ticket else str(params.get("title", "") or ""),
        "description": ticket.description
        if ticket
        else str(params.get("description", "") or ""),
        "subscriber_id": str(ticket.subscriber_id)
        if ticket and ticket.subscriber_id
        else str(params.get("subscriber_id", "") or ""),
        "customer_account_id": str(ticket.customer_account_id)
        if ticket and ticket.customer_account_id
        else str(params.get("customer_account_id", "") or ""),
        "customer_person_id": str(ticket.customer_person_id)
        if ticket and ticket.customer_person_id
        else str(params.get("customer_person_id", "") or ""),
        "region": ticket.region if ticket else str(params.get("region", "") or ""),
        "ticket_type": ticket.ticket_type
        if ticket
        else str(params.get("ticket_type", "") or ""),
        "priority": ticket.priority.value
        if ticket
        else str(params.get("priority", TicketPriority.normal.value) or ""),
        "channel": ticket.channel.value
        if ticket
        else str(params.get("channel", TicketChannel.web.value) or ""),
        "status": ticket.status.value
        if ticket
        else str(params.get("status", TicketStatus.open.value) or ""),
        "due_at": ticket.due_at.strftime("%Y-%m-%dT%H:%M")
        if ticket and ticket.due_at
        else "",
        "tags": ",".join(ticket.tags or [])
        if ticket
        else str(params.get("tags", "") or ""),
        "related_outage_ticket_id": str(
            params.get("related_outage_ticket_id", "") or ""
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
        "service_team_options": service_team_options(),
        "people_options": people,
        "prefill": prefill,
    }


def build_ticket_edit_page_context(
    db: Session,
    *,
    query_params: Mapping[str, object] | None,
    ticket_lookup: str,
) -> dict:
    ticket = support_service.tickets.get_by_lookup(db, ticket_lookup)
    context = build_ticket_form_context(db, query_params=query_params, ticket=ticket)
    context.update(
        {
            "page_title": "Edit Ticket",
            "form_mode": "edit",
            "ticket": ticket,
        }
    )
    return context


def build_ticket_create_payload(**kwargs) -> TicketCreate:
    tags = kwargs.pop("tags", None)
    assignee_person_ids = kwargs.pop("assignee_person_ids", [])
    return TicketCreate(
        title=kwargs["title"],
        description=kwargs["description"] or None,
        subscriber_id=parse_uuid_or_none(kwargs.get("subscriber_id")),
        customer_account_id=parse_uuid_or_none(kwargs.get("customer_account_id")),
        customer_person_id=parse_uuid_or_none(kwargs.get("customer_person_id")),
        created_by_person_id=parse_uuid_or_none(kwargs.get("actor_id")),
        region=kwargs.get("region") or None,
        technician_person_id=parse_uuid_or_none(kwargs.get("technician_person_id")),
        ticket_manager_person_id=parse_uuid_or_none(
            kwargs.get("ticket_manager_person_id")
        ),
        site_coordinator_person_id=parse_uuid_or_none(
            kwargs.get("site_coordinator_person_id")
        ),
        service_team_id=parse_uuid_or_none(kwargs.get("service_team_id")),
        ticket_type=kwargs.get("ticket_type") or None,
        priority=kwargs["priority"],
        channel=kwargs["channel"],
        status=kwargs["status"],
        due_at=parse_dt_or_none(kwargs.get("due_at")),
        tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
        assignee_person_ids=[
            uid
            for uid in (parse_uuid_or_none(item) for item in assignee_person_ids)
            if uid
        ],
        related_outage_ticket_id=parse_uuid_or_none(
            kwargs.get("related_outage_ticket_id")
        ),
    )


def build_ticket_update_payload(**kwargs) -> TicketUpdate:
    tags = kwargs.pop("tags", None)
    assignee_person_ids = kwargs.pop("assignee_person_ids", [])
    return TicketUpdate(
        title=kwargs["title"],
        description=kwargs["description"] or None,
        subscriber_id=parse_uuid_or_none(kwargs.get("subscriber_id")),
        customer_account_id=parse_uuid_or_none(kwargs.get("customer_account_id")),
        customer_person_id=parse_uuid_or_none(kwargs.get("customer_person_id")),
        region=kwargs.get("region") or None,
        status=kwargs["status"],
        priority=kwargs["priority"],
        channel=kwargs["channel"],
        ticket_type=kwargs.get("ticket_type") or None,
        due_at=parse_dt_or_none(kwargs.get("due_at")),
        tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
        technician_person_id=parse_uuid_or_none(kwargs.get("technician_person_id")),
        ticket_manager_person_id=parse_uuid_or_none(
            kwargs.get("ticket_manager_person_id")
        ),
        site_coordinator_person_id=parse_uuid_or_none(
            kwargs.get("site_coordinator_person_id")
        ),
        service_team_id=parse_uuid_or_none(kwargs.get("service_team_id")),
        assignee_person_ids=[
            uid
            for uid in (parse_uuid_or_none(item) for item in assignee_person_ids)
            if uid
        ],
    )


def build_ticket_comment_payload(
    *, body: str, is_internal: bool, actor_id: str | None, uploaded: list[dict]
) -> TicketCommentCreate:
    return TicketCommentCreate(
        body=body,
        is_internal=is_internal,
        author_person_id=parse_uuid_or_none(actor_id),
        attachments=[AttachmentMeta(**item) for item in uploaded],
    )


def create_ticket_from_form(
    db: Session,
    *,
    request,
    actor_id: str | None,
    attachments: list,
    **form,
):
    """Create a support ticket from web form values and attach uploaded files."""
    payload = build_ticket_create_payload(actor_id=actor_id, **form)
    ticket = support_service.tickets.create(
        db, payload, actor_id=actor_id, request=request
    )
    if attachments:
        uploaded = upload_ticket_attachments(
            db,
            ticket_id=str(ticket.id),
            attachments=attachments,
            entity_type="support_ticket_attachment",
            actor_id=actor_id,
        )
        support_service.tickets.add_attachments(db, str(ticket.id), uploaded)
    return ticket


def update_ticket_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
    **form,
):
    payload = build_ticket_update_payload(**form)
    return support_service.tickets.update(
        db,
        ticket_id,
        payload,
        actor_id=actor_id,
        request=request,
    )


def add_ticket_comment_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
    body: str,
    is_internal: bool,
    attachments: list,
):
    uploaded = upload_ticket_attachments(
        db,
        ticket_id=ticket_id,
        attachments=attachments,
        entity_type="support_ticket_comment_attachment",
        actor_id=actor_id,
    )
    payload = build_ticket_comment_payload(
        body=body,
        is_internal=is_internal,
        actor_id=actor_id,
        uploaded=uploaded,
    )
    return support_service.tickets.create_comment(
        db,
        ticket_id,
        payload,
        actor_id=actor_id,
        request=request,
    )


def auto_assign_ticket(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
):
    return support_service.tickets.manual_auto_assign(
        db,
        ticket_id,
        actor_id=actor_id,
        request=request,
    )


def link_ticket_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    to_ticket_id: str,
    link_type: str,
    actor_id: str | None,
):
    payload = TicketLinkCreate(to_ticket_id=UUID(to_ticket_id), link_type=link_type)
    return support_service.tickets.link_ticket(
        db,
        from_ticket_id=ticket_id,
        to_ticket_id=str(payload.to_ticket_id),
        link_type=payload.link_type,
        actor_id=actor_id,
        request=request,
    )


def merge_ticket_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    target_ticket_id: str,
    reason: str | None,
    actor_id: str | None,
):
    return support_service.tickets.merge(
        db,
        ticket_id,
        TicketMergeRequest(target_ticket_id=UUID(target_ticket_id), reason=reason),
        actor_id=actor_id,
        request=request,
    )


def delete_ticket(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
) -> None:
    support_service.tickets.soft_delete(
        db,
        ticket_id,
        actor_id=actor_id,
        request=request,
    )


def delete_ticket_hx_headers() -> dict[str, str]:
    return {
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


def build_tickets_list_context(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    ticket_type: str | None,
    assigned_to_me: bool,
    actor_id: str | None,
    project_manager_person_id: str | None,
    site_coordinator_person_id: str | None,
    subscriber_id: str | None,
    order_by: str,
    order_dir: str,
    page: int,
    per_page: int,
    visible_columns_cookie: str | None,
) -> dict:
    offset = (page - 1) * per_page
    rows = support_service.tickets.list(
        db,
        search=search,
        status=status,
        ticket_type=ticket_type,
        assigned_to_person_id=actor_id if assigned_to_me else None,
        project_manager_person_id=project_manager_person_id,
        site_coordinator_person_id=site_coordinator_person_id,
        subscriber_id=subscriber_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=per_page,
        offset=offset,
    )
    people = support_service.list_people(db)
    return {
        "tickets": rows,
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
        "has_next_page": len(rows) >= per_page,
        "status_totals": support_service.status_totals(db),
        "visible_columns": visible_ticket_columns(visible_columns_cookie),
        "ticket_columns": TICKET_COLUMNS,
        "all_statuses": [item.value for item in TicketStatus],
        "all_priorities": [item.value for item in TicketPriority],
        "ticket_type_options": support_service.ticket_types(db),
        "people_options": people,
        "people_lookup": {item["id"]: item["label"] for item in people},
    }


def build_ticket_detail_context(db: Session, *, ticket_lookup: str) -> dict:
    from app.services.audit_helpers import build_audit_activities

    ticket = support_service.tickets.get_by_lookup(db, ticket_lookup)
    return {
        "ticket": ticket,
        "comments": support_service.ticket_comments.list(
            db, str(ticket.id), limit=500, offset=0
        ),
        "sla_events": support_service.ticket_sla_events.list(
            db, str(ticket.id), limit=200, offset=0
        ),
        "ticket_links": support_service.tickets.list_links(
            db, str(ticket.id), limit=100
        ),
        "activities": build_audit_activities(
            db, "support_ticket", str(ticket.id), limit=100
        ),
        "all_statuses": [item.value for item in TicketStatus],
        "all_priorities": [item.value for item in TicketPriority],
        "all_channels": [item.value for item in TicketChannel],
        "people_options": support_service.list_people(db),
        "service_team_options": service_team_options(),
        "is_merged_source": bool(
            ticket.merged_into_ticket_id or ticket.status.value == "merged"
        ),
    }
