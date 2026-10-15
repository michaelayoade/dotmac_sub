"""Service helpers for admin system audit pages."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import cast

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
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
    PageMeta,
)
from app.timezone import APP_TIMEZONE

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
        ListFieldDefinition("actor_type", "Actor type", filterable=True),
        ListFieldDefinition("action", "Action", filterable=True),
        ListFieldDefinition("entity_type", "Module", filterable=True),
        ListFieldDefinition("result", "Result", filterable=True),
        ListFieldDefinition("period", "Period", filterable=True),
        ListFieldDefinition("date_from", "From", filterable=True),
        ListFieldDefinition("date_to", "To", filterable=True),
        ListFieldDefinition("entity_id", "Record ID", filterable=True),
        ListFieldDefinition("entity_id_missing", "Missing record ID", filterable=True),
        ListFieldDefinition("request_id", "Request ID", filterable=True),
        ListFieldDefinition(
            "request_id_missing", "Missing request ID", filterable=True
        ),
        ListFieldDefinition("occurred_at", "Occurred", sortable=True),
    ),
    default_sort="occurred_at",
    default_sort_dir="desc",
    default_per_page=50,
)


def build_audit_list_query(
    *,
    actor_id: str | None = None,
    actor_type: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    result: str | None = None,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    entity_id: str | None = None,
    entity_id_missing: bool = False,
    request_id: str | None = None,
    request_id_missing: bool = False,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalise loose audit-log request params through the page contract.

    Rejects unsupported sort fields / page sizes and drops blank filters, so
    the read owner receives an already-validated query.
    """
    normalized_actor_type = (actor_type or "").strip()
    if normalized_actor_type:
        AuditActorType(normalized_actor_type)
    normalized_result = (result or "").strip()
    if normalized_result and normalized_result not in {"success", "failed"}:
        raise ValueError("unsupported audit result")
    normalized_period = (period or "").strip()
    if normalized_period and normalized_period not in {
        "today",
        "last_7_days",
        "last_30_days",
        "custom",
    }:
        raise ValueError("unsupported audit period")
    parsed_from = _parse_iso_date(date_from)
    parsed_to = _parse_iso_date(date_to)
    if not normalized_period and (parsed_from or parsed_to):
        normalized_period = "custom"
    if normalized_period == "custom" and not (parsed_from or parsed_to):
        raise ValueError("custom audit period needs a start or end date")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise ValueError("audit start date cannot be after end date")
    if entity_id and entity_id_missing:
        raise ValueError("record ID and missing-record-ID filters conflict")
    if request_id and request_id_missing:
        raise ValueError("request ID and missing-request-ID filters conflict")

    return AUDIT_EVENTS_LIST_DEFINITION.build_query(
        search=None,
        filters={
            "actor_id": actor_id,
            "actor_type": normalized_actor_type,
            "action": action,
            "entity_type": entity_type,
            "result": normalized_result,
            "period": normalized_period,
            "date_from": date_from,
            "date_to": date_to,
            "entity_id": entity_id,
            "entity_id_missing": "1" if entity_id_missing else None,
            "request_id": request_id,
            "request_id_missing": "1" if request_id_missing else None,
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def _parse_iso_date(value: str | None) -> date | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return date.fromisoformat(normalized)


def _date_bounds(query: ListQuery) -> tuple[datetime | None, datetime | None]:
    period = query.filter_value("period")
    today = datetime.now(APP_TIMEZONE).date()
    start: date | None = None
    end: date | None = None
    if period == "today":
        start = end = today
    elif period == "last_7_days":
        start, end = today - timedelta(days=6), today
    elif period == "last_30_days":
        start, end = today - timedelta(days=29), today
    elif period == "custom":
        start = _parse_iso_date(query.filter_value("date_from"))
        end = _parse_iso_date(query.filter_value("date_to"))

    occurred_from = (
        datetime.combine(start, time.min, tzinfo=APP_TIMEZONE).astimezone(UTC)
        if start
        else None
    )
    occurred_before = (
        datetime.combine(
            end + timedelta(days=1), time.min, tzinfo=APP_TIMEZONE
        ).astimezone(UTC)
        if end
        else None
    )
    return occurred_from, occurred_before


def get_audit_filter_options(
    db: Session,
    *,
    selected_entity_type: str | None = None,
) -> dict[str, object]:
    """Return display-ready suggestions sourced from recorded audit values."""

    options = audit_service.audit_events.filter_options(
        db,
        audit_service.AuditEventFilterOptionsQuery(),
    )
    entity_types = options.entity_types
    if selected_entity_type and selected_entity_type not in entity_types:
        entity_types = tuple(sorted((*entity_types, selected_entity_type)))
    return {
        "action_options": options.actions,
        "module_options": tuple(
            {"value": value, "label": humanize_entity(value)} for value in entity_types
        ),
        "actor_type_options": tuple(
            {"value": value.value, "label": value.value.replace("_", " ").title()}
            for value in AuditActorType
        ),
    }


def get_audit_page_data(db: Session, query: ListQuery) -> dict[str, object]:
    """Return audit events view rows and pagination totals for one page.

    Reads filters, ordering and pagination from the validated ``ListQuery``
    (built via :func:`build_audit_list_query`); the caller is a thin adapter.
    """
    # The Actor box searches the resolved label (person name / API-key label)
    # and also matches an exact actor id, so staff can paste a uuid or type a
    # name. It is no longer a uuid-only exact filter.
    actor_term = (query.filter_value("actor_id") or "").strip() or None
    actor_type_value = query.filter_value("actor_type")
    action = query.filter_value("action")
    entity_type = query.filter_value("entity_type")
    entity_id = query.filter_value("entity_id")
    request_id = query.filter_value("request_id")
    result_value = query.filter_value("result")
    occurred_from, occurred_before = _date_bounds(query)

    def owner_query(list_query: ListQuery) -> audit_service.AuditEventPageQuery:
        return audit_service.AuditEventPageQuery(
            actor_search=actor_term,
            actor_type=AuditActorType(actor_type_value) if actor_type_value else None,
            action=action if action else None,
            entity_type=entity_type if entity_type else None,
            entity_id=entity_id,
            entity_id_missing=query.filter_value("entity_id_missing") == "1",
            request_id=request_id,
            request_id_missing=query.filter_value("request_id_missing") == "1",
            is_success=(
                True
                if result_value == "success"
                else False
                if result_value == "failed"
                else None
            ),
            status_code=None,
            is_active=None,
            occurred_from=occurred_from,
            occurred_before=occurred_before,
            order_by=cast(audit_service.AuditEventOrderField, list_query.sort_by),
            order_dir=cast(audit_service.AuditEventOrderDirection, list_query.sort_dir),
            limit=list_query.per_page,
            offset=list_query.offset,
        )

    result = audit_service.audit_events.query_page(db, owner_query(query))
    page_meta = PageMeta.from_query(query, result.total)
    if page_meta.page != query.page:
        query = query.with_page(page_meta.page)
        result = audit_service.audit_events.query_page(db, owner_query(query))
        page_meta = PageMeta.from_query(query, result.total)
    events = result.events

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
        module_label = (
            humanize_entity(event.entity_type)
            if event.entity_type
            else "Unknown module"
        )
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
                "module_label": module_label,
                "is_success": event.is_success,
                "status_code": event.status_code,
            }
        )

    return {
        "events": event_views,
        "list_query": query,
        "page_meta": page_meta,
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": page_meta.total_items,
        "total_pages": page_meta.total_pages,
        "filter_state": dict(query.filters),
        "has_active_filters": bool(query.filters),
    }


def _resolve_actor_name(event, people: dict[str, object]) -> str:
    return resolve_actor_name(event, people)
