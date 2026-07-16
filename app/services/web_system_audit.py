"""Service helpers for admin system audit pages."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.services import audit as audit_service
from app.services.audit_helpers import (
    extract_changes,
    format_audit_datetime,
    format_changes,
    humanize_action,
    humanize_entity,
    load_audit_actor_subscribers,
    resolve_actor_name,
)
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
)

logger = logging.getLogger(__name__)

# UI page contract for the admin audit-log list. This is the projection-boundary
# owner: it declares which columns are filterable and sortable, the default
# ordering, and the allowed page sizes. The route renders and submits through
# this contract; it does not decide relevance or ordering. Only occurred_at is
# sortable because the audit read owner (audit_events.list) orders on it.
AUDIT_EVENTS_LIST_DEFINITION = ListDefinition(
    key="audit_events",
    fields=(
        ListFieldDefinition("actor_id", "Actor", filterable=True),
        ListFieldDefinition("action", "Action", filterable=True),
        ListFieldDefinition("entity_type", "Entity type", filterable=True),
        ListFieldDefinition("occurred_at", "Occurred", sortable=True),
    ),
    default_sort="occurred_at",
    default_sort_dir="desc",
    default_per_page=50,
)


def build_audit_list_query(
    *,
    actor_id: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalise loose audit-log request params through the page contract.

    Rejects unsupported sort fields / page sizes and drops blank filters, so
    the read owner receives an already-validated query.
    """
    return AUDIT_EVENTS_LIST_DEFINITION.build_query(
        search=None,
        filters={
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def get_audit_page_data(db: Session, query: ListQuery) -> dict[str, object]:
    """Return audit events view rows and pagination totals for one page.

    Reads filters, ordering and pagination from the validated ``ListQuery``
    (built via :func:`build_audit_list_query`); the caller is a thin adapter.
    """
    actor_id = query.filter_value("actor_id")
    action = query.filter_value("action")
    entity_type = query.filter_value("entity_type")
    page = query.page
    per_page = query.per_page
    offset = query.offset
    normalized_actor_id = _normalize_actor_id(actor_id)

    events = audit_service.audit_events.list(
        db=db,
        actor_id=normalized_actor_id,
        actor_type=None,
        action=action if action else None,
        entity_type=entity_type if entity_type else None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by=query.sort_by,
        order_dir=query.sort_dir,
        limit=per_page,
        offset=offset,
    )

    try:
        people = load_audit_actor_subscribers(db, events)
    except Exception:
        people = {}

    event_views = []
    for event in events:
        actor_name = _resolve_actor_name(event, people)
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(event.action)
        entity_label = humanize_entity(event.entity_type, event.entity_id)
        event_views.append(
            {
                "occurred_at": event.occurred_at,
                "occurred_at_display": format_audit_datetime(
                    event.occurred_at, "%b %d, %Y %H:%M"
                ),
                "actor_name": actor_name,
                "actor_id": event.actor_id,
                "action": event.action,
                "action_label": action_label,
                "action_detail": change_summary,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "entity_label": entity_label,
                "is_success": event.is_success,
                "status_code": event.status_code,
            }
        )

    total_stmt = (
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.is_active.is_(True))
    )
    if normalized_actor_id:
        total_stmt = total_stmt.where(AuditEvent.actor_id == UUID(normalized_actor_id))
    if action:
        total_stmt = total_stmt.where(AuditEvent.action == action)
    if entity_type:
        total_stmt = total_stmt.where(AuditEvent.entity_type == entity_type)

    total = db.scalar(total_stmt) or 0
    total_pages = (total + per_page - 1) // per_page

    return {
        "events": event_views,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def _normalize_actor_id(actor_id: str | None) -> str | None:
    if not actor_id:
        return None
    value = actor_id.strip()
    if not value:
        return None
    if value.lower() in {"none", "null", "undefined"}:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _resolve_actor_name(event, people: dict[str, object]) -> str:
    return resolve_actor_name(event, people)
