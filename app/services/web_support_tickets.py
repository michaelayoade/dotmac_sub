"""Web helpers for admin support ticket routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketChannel, TicketCommentAuthorType
from app.schemas.support import (
    AttachmentMeta,
    TicketCommentCreate,
    TicketCreate,
    TicketLinkCreate,
    TicketMergeRequest,
    TicketUpdate,
)
from app.services import support as support_service
from app.services import support_ticket_settings as support_ticket_settings_service
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


def _non_empty_ids(values: list[object | None]) -> list[str]:
    ids: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        if text not in ids:
            ids.append(text)
    return ids


def _label_lookup(options: list[dict[str, str]]) -> dict[str, str]:
    return {item["id"]: item["label"] for item in options if item.get("id")}


def _service_team_lookup() -> dict[str, str]:
    return {item["id"]: item["label"] for item in service_team_options()}


def _append_missing_option(options: list[str], value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text or text in options:
        return options
    return [*options, text]


def _status_summary_cards(db: Session) -> list[dict[str, str | int]]:
    totals = support_service.status_totals(db)
    closed_count = int(totals.get("closed", 0))
    canceled_count = int(totals.get("canceled", 0))
    open_count = sum(
        int(count)
        for status, count in totals.items()
        if str(status).strip() not in {"closed", "canceled"}
    )

    return [
        {
            "value": "open",
            "label": "Open",
            "count": open_count,
            "href": "/admin/support/tickets?status=open",
            "color": "emerald",
        },
        {
            "value": "closed",
            "label": "Closed",
            "count": closed_count,
            "href": "/admin/support/tickets?status=closed",
            "color": "slate",
        },
        {
            "value": "canceled",
            "label": "Cancelled",
            "count": canceled_count,
            "href": "/admin/support/tickets?status=canceled",
            "color": "red",
        },
    ]


def _resolve_uploaded_by_subscriber_id(db: Session, actor_id: str | None) -> str | None:
    uid = parse_uuid_or_none(actor_id)
    if not uid:
        return None
    return str(uid) if db.get(Subscriber, uid) else None


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
    uploaded_by = _resolve_uploaded_by_subscriber_id(db, actor_id)
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
                uploaded_by=uploaded_by,
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
    status_options = support_ticket_settings_service.list_status_options(db)
    priority_options = support_ticket_settings_service.list_priority_options(db)
    ticket_type_options = support_ticket_settings_service.list_ticket_type_options(db)
    current_assignees = [
        str(row.person_id)
        for row in (ticket.assignees if ticket and ticket.assignees else [])
        if row.person_id
    ]
    assignment_ids = current_assignees + _non_empty_ids(
        [
            ticket.technician_person_id if ticket else None,
            ticket.ticket_manager_person_id if ticket else None,
            ticket.site_coordinator_person_id if ticket else None,
        ]
    )
    staff = support_service.list_assignment_people(db, include_ids=assignment_ids)
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
        "priority": ticket.priority
        if ticket
        else str(
            params.get("priority", support_ticket_settings_service.default_priority(db))
            or ""
        ),
        "channel": ticket.channel.value
        if ticket
        else str(params.get("channel", TicketChannel.web.value) or ""),
        "status": ticket.status
        if ticket
        else str(
            params.get("status", support_ticket_settings_service.default_status(db))
            or ""
        ),
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
    customer_person_id = str(prefill["customer_person_id"] or "")
    subscriber_id_value = str(prefill["subscriber_id"] or "")
    customer_person = support_service.person_option(db, customer_person_id)
    subscriber_person = support_service.person_option(db, subscriber_id_value)
    selected_person = subscriber_person or customer_person or {}
    prefill["customer_person_label"] = (
        customer_person["label"] if customer_person else ""
    )
    prefill["subscriber_label"] = (
        subscriber_person["label"] if subscriber_person else ""
    )
    status_options = _append_missing_option(
        status_options, str(prefill["status"] or "")
    )
    priority_options = _append_missing_option(
        priority_options, str(prefill["priority"] or "")
    )
    ticket_type_options = _append_missing_option(
        ticket_type_options, str(prefill["ticket_type"] or "")
    )
    return {
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "all_channels": [item.value for item in TicketChannel],
        "region_options": support_service.regions(db),
        "ticket_type_options": ticket_type_options,
        "service_team_options": service_team_options(),
        "staff_options": staff,
        "subscriber_options": support_service.list_people(
            db,
            include_ids=_non_empty_ids(
                [prefill["customer_person_id"], prefill["subscriber_id"]]
            ),
        ),
        "selected_person": selected_person,
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
        created_by_person_id=None,
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
        author_type=TicketCommentAuthorType.staff,
        author_system_user_id=parse_uuid_or_none(actor_id),
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


def quick_update_ticket(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
    fields: dict,
):
    """Apply a small set of fields to a ticket (status, technician, etc.).

    Surfaces invalid client input as HTTP 400 rather than a 500 from a bare
    ValueError / pydantic ValidationError.
    """
    from fastapi import HTTPException
    from pydantic import ValidationError

    uuid_fields = {
        "technician_person_id",
        "ticket_manager_person_id",
        "site_coordinator_person_id",
        "service_team_id",
        "assigned_to_person_id",
    }
    payload_data: dict = {}
    for key, value in fields.items():
        if value in (None, ""):
            continue
        if key in uuid_fields:
            try:
                payload_data[key] = UUID(str(value))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"{key} must be a valid UUID"
                ) from exc
        elif key in ("status", "priority"):
            payload_data[key] = str(value).strip()
        else:
            payload_data[key] = value
    try:
        payload = TicketUpdate(**payload_data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc
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


def _identity_resolution_summary(ticket) -> dict[str, str] | None:
    metadata = dict(getattr(ticket, "metadata_", None) or {})
    resolution = metadata.get("identity_resolution")
    if not isinstance(resolution, dict):
        return None
    status = str(resolution.get("status") or "").strip()
    matched_via = str(resolution.get("matched_via") or "").strip()
    matched_field = str(resolution.get("matched_field") or "").strip()
    confidence = str(resolution.get("match_confidence") or "").strip().upper()
    confidence_suffix = f", {confidence} confidence" if confidence else ""
    if status == "matched" and matched_via == "subscriber_contact":
        detail = "Matched via subscriber contact"
        if matched_field:
            detail = f"{detail} ({matched_field})"
        if confidence_suffix:
            detail = f"{detail}{confidence_suffix}"
        return {"status": status, "detail": detail}
    if status == "matched" and matched_via:
        detail = f"Matched via {matched_via.replace('_', ' ')}"
        if matched_field:
            detail = f"{detail} ({matched_field})"
        if confidence_suffix:
            detail = f"{detail}{confidence_suffix}"
        return {"status": status, "detail": detail}
    if status == "ambiguous":
        return {
            "status": status,
            "detail": "Inbound identity is ambiguous and requires manual review",
        }
    if status == "unmatched" and metadata.get("manual_review_required"):
        return {
            "status": status,
            "detail": "Inbound identity requires manual review before account actions",
        }
    return None


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
    status_options = support_ticket_settings_service.list_status_options(db)
    priority_options = support_ticket_settings_service.list_priority_options(db)
    status_options = _append_missing_option(status_options, status)
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
    assignment_ids: list[object | None] = []
    subscriber_ids: list[object | None] = [subscriber_id]
    for ticket in rows:
        assignment_ids.extend(
            [
                ticket.assigned_to_person_id,
                ticket.technician_person_id,
                ticket.ticket_manager_person_id,
                ticket.site_coordinator_person_id,
            ]
        )
        subscriber_ids.extend(
            [
                ticket.customer_person_id,
                ticket.customer_account_id,
                ticket.subscriber_id,
            ]
        )
    staff = support_service.list_assignment_people(
        db,
        include_ids=_non_empty_ids(
            assignment_ids
            + [project_manager_person_id, site_coordinator_person_id, actor_id]
        ),
    )
    subscribers = support_service.list_people(
        db,
        include_ids=_non_empty_ids(subscriber_ids),
    )
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
        "status_summary_cards": _status_summary_cards(db),
        "visible_columns": visible_ticket_columns(visible_columns_cookie),
        "ticket_columns": TICKET_COLUMNS,
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "ticket_type_options": support_service.ticket_types(db),
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "subscriber_options": subscribers,
        "subscriber_lookup": _label_lookup(subscribers),
    }


def build_ticket_detail_context(db: Session, *, ticket_lookup: str) -> dict:
    from app.services.audit_helpers import build_audit_activities

    status_options = support_ticket_settings_service.list_status_options(db)
    priority_options = support_ticket_settings_service.list_priority_options(db)
    ticket = support_service.tickets.get_by_lookup(db, ticket_lookup)
    comments = support_service.ticket_comments.list(db, str(ticket.id), limit=500, offset=0)
    status_options = _append_missing_option(status_options, ticket.status)
    priority_options = _append_missing_option(priority_options, ticket.priority)
    staff = support_service.list_assignment_people(
        db,
        include_ids=_non_empty_ids(
            [
                ticket.assigned_to_person_id,
                ticket.technician_person_id,
                ticket.ticket_manager_person_id,
                ticket.site_coordinator_person_id,
                *[row.person_id for row in (ticket.assignees or [])],
                *[
                    comment.author_system_user_id
                    for comment in comments
                    if getattr(comment, "author_system_user_id", None)
                ],
            ]
        ),
    )
    subscribers = support_service.list_people(
        db,
        include_ids=_non_empty_ids(
            [
                ticket.customer_person_id,
                ticket.customer_account_id,
                ticket.subscriber_id,
                *[
                    comment.author_person_id
                    for comment in comments
                    if getattr(comment, "author_person_id", None)
                ],
            ]
        ),
    )
    return {
        "ticket": ticket,
        "comments": comments,
        "sla_events": support_service.ticket_sla_events.list(
            db, str(ticket.id), limit=200, offset=0
        ),
        "ticket_links": support_service.tickets.list_links(
            db, str(ticket.id), limit=100
        ),
        "activities": build_audit_activities(
            db, "support_ticket", str(ticket.id), limit=100
        ),
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "all_channels": [item.value for item in TicketChannel],
        "people_options": subscribers,
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "subscriber_lookup": _label_lookup(subscribers),
        "service_team_options": service_team_options(),
        "service_team_lookup": _service_team_lookup(),
        "is_merged_source": bool(
            ticket.merged_into_ticket_id
            or support_ticket_settings_service.status_is_merged(ticket.status)
        ),
        "identity_resolution": _identity_resolution_summary(ticket),
    }
