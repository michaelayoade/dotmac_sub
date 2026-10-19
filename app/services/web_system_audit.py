"""Service helpers for admin system audit pages."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from typing import Literal, cast

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.schemas.audit import AuditEventRead
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

AuditExportFormat = Literal["csv", "json"]
AUDIT_EXPORT_MAX_ROWS = 10_000


class AuditFilterValidationError(ValueError):
    """A safe, user-facing audit-filter validation failure."""


class AuditExportTooLargeError(ValueError):
    """The filtered export exceeds the explicit safe row limit."""


@dataclass(frozen=True, slots=True)
class AuditExportDocument:
    content: bytes
    media_type: str
    filename: str
    row_count: int


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
        try:
            AuditActorType(normalized_actor_type)
        except ValueError as exc:
            raise AuditFilterValidationError("Choose a valid actor type.") from exc
    normalized_result = (result or "").strip()
    if normalized_result and normalized_result not in {"success", "failed"}:
        raise AuditFilterValidationError("Choose a valid result.")
    normalized_period = (period or "").strip()
    if normalized_period and normalized_period not in {
        "today",
        "last_7_days",
        "last_30_days",
        "custom",
    }:
        raise AuditFilterValidationError("Choose a valid date range.")
    if normalized_period in {"today", "last_7_days", "last_30_days"}:
        # Presets own their date bounds; stale custom values must not remain in
        # the URL or suggest that they also affect the result.
        date_from = None
        date_to = None
        parsed_from = None
        parsed_to = None
    else:
        parsed_from = _parse_iso_date(date_from)
        parsed_to = _parse_iso_date(date_to)
    if not normalized_period and (parsed_from or parsed_to):
        normalized_period = "custom"
    if normalized_period == "custom" and not (parsed_from or parsed_to):
        raise AuditFilterValidationError("Choose at least one date for a custom range.")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise AuditFilterValidationError(
            "The From date cannot be later than the To date."
        )
    if entity_id and entity_id_missing:
        raise AuditFilterValidationError(
            "Use either a Record ID or Missing record ID, not both."
        )
    if request_id and request_id_missing:
        raise AuditFilterValidationError(
            "Use either a Request ID or Missing request ID, not both."
        )

    try:
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
    except ValueError as exc:
        raise AuditFilterValidationError(
            "One or more filter options are invalid."
        ) from exc


def _parse_iso_date(value: str | None) -> date | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise AuditFilterValidationError("Enter dates in YYYY-MM-DD format.") from exc


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
    selected_action: str | None = None,
) -> dict[str, object]:
    """Return display-ready suggestions sourced from recorded audit values."""

    options = audit_service.audit_events.filter_options(
        db,
        audit_service.AuditEventFilterOptionsQuery(),
    )
    entity_types = options.entity_types
    if selected_entity_type and selected_entity_type not in entity_types:
        entity_types = tuple(sorted((*entity_types, selected_entity_type)))
    actions = options.actions
    if selected_action and selected_action not in actions:
        actions = tuple(sorted((*actions, selected_action)))
    action_options = tuple(
        {
            "value": value,
            "label": humanize_action(value),
            "display": f"{humanize_action(value)} - {value}",
        }
        for value in actions
    )
    selected_action_display = next(
        (
            option["display"]
            for option in action_options
            if option["value"] == selected_action
        ),
        selected_action or "",
    )
    return {
        "action_options": action_options,
        "selected_action_display": selected_action_display,
        "module_options": tuple(
            {"value": value, "label": humanize_entity(value)} for value in entity_types
        ),
        "actor_type_options": tuple(
            {"value": value.value, "label": value.value.replace("_", " ").title()}
            for value in AuditActorType
        ),
    }


def _owner_query(
    query: ListQuery,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> audit_service.AuditEventPageQuery:
    """Translate the UI contract once for both list and export reads."""

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
    return audit_service.AuditEventPageQuery(
        actor_search=actor_term,
        actor_type=AuditActorType(actor_type_value) if actor_type_value else None,
        action=action or None,
        entity_type=entity_type or None,
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
        order_by=cast(audit_service.AuditEventOrderField, query.sort_by),
        order_dir=cast(audit_service.AuditEventOrderDirection, query.sort_dir),
        limit=limit if limit is not None else query.per_page,
        offset=offset if offset is not None else query.offset,
    )


def _event_views(
    db: Session,
    events: tuple[AuditEventRead, ...],
) -> list[dict[str, object]]:
    """Build the one redacted projection shared by screen and exports."""

    try:
        people = load_audit_actor_subscribers(db, list(events))
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
        safe_details = audit_service.redact_audit_details(event.details or metadata)
        event_views.append(
            {
                "occurred_at": event.occurred_at,
                "occurred_at_display": format_audit_datetime(
                    event.occurred_at, "%b %d, %Y %H:%M"
                ),
                "actor_name": actor_name,
                "actor_id": event.actor_id,
                "actor_type": event.actor_type.value,
                "action": event.action,
                "action_label": action_label,
                "action_detail": change_summary,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "entity_label": entity_label,
                "module_label": module_label,
                "request_id": event.request_id,
                "is_success": event.is_success,
                "status_code": event.status_code,
                "ip_address": event.ip_address,
                "safe_details": safe_details,
            }
        )

    return event_views


def get_audit_page_data(db: Session, query: ListQuery) -> dict[str, object]:
    """Return audit events view rows and pagination totals for one page.

    Reads filters, ordering and pagination from the validated ``ListQuery``
    (built via :func:`build_audit_list_query`); the caller is a thin adapter.
    """
    result = audit_service.audit_events.query_page(db, _owner_query(query))
    page_meta = PageMeta.from_query(query, result.total)
    if page_meta.page != query.page:
        query = query.with_page(page_meta.page)
        result = audit_service.audit_events.query_page(db, _owner_query(query))
        page_meta = PageMeta.from_query(query, result.total)
    event_views = _event_views(db, result.events)

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


def _export_row(event: dict[str, object]) -> dict[str, object]:
    occurred_at = event["occurred_at"]
    return {
        "occurred_at": (
            occurred_at.isoformat() if isinstance(occurred_at, datetime) else ""
        ),
        "actor": event["actor_name"],
        "actor_type": event["actor_type"],
        "action": event["action"],
        "module": event["entity_type"] or "Unknown module",
        "record_id": event["entity_id"] or "Not recorded",
        "request_id": event["request_id"] or "Not recorded",
        "result": "successful" if event["is_success"] else "failed",
        "status_code": event["status_code"],
        "ip_address": event["ip_address"] or "Not recorded",
        "details": event["safe_details"],
    }


def _csv_safe(value: object) -> str | int | float | bool | None:
    if not isinstance(value, str):
        return cast(str | int | float | bool | None, value)
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def build_audit_export(
    db: Session,
    *,
    query: ListQuery,
    export_format: AuditExportFormat,
) -> AuditExportDocument:
    """Build a bounded export from the exact canonical list filters."""

    probe = audit_service.audit_events.query_page(
        db,
        _owner_query(query, limit=1, offset=0),
    )
    if probe.total > AUDIT_EXPORT_MAX_ROWS:
        raise AuditExportTooLargeError(
            f"This export contains {probe.total:,} events. Narrow the filters "
            f"to {AUDIT_EXPORT_MAX_ROWS:,} events or fewer."
        )
    result = (
        probe
        if probe.total <= 1
        else audit_service.audit_events.query_page(
            db,
            _owner_query(query, limit=AUDIT_EXPORT_MAX_ROWS, offset=0),
        )
    )
    rows = tuple(_export_row(event) for event in _event_views(db, result.events))
    generated_at = datetime.now(UTC)
    filename_base = generated_at.strftime("audit-log-%Y%m%d-%H%M%S")

    if export_format == "json":
        payload = {
            "generated_at": generated_at.isoformat(),
            "row_count": len(rows),
            "filters": dict(query.filters),
            "events": rows,
        }
        return AuditExportDocument(
            content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            media_type="application/json",
            filename=f"{filename_base}.json",
            row_count=len(rows),
        )

    output = StringIO(newline="")
    fieldnames = (
        tuple(rows[0])
        if rows
        else (
            "occurred_at",
            "actor",
            "actor_type",
            "action",
            "module",
            "record_id",
            "request_id",
            "result",
            "status_code",
            "ip_address",
            "details",
        )
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        csv_row = {
            key: _csv_safe(
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if key == "details"
                else value
            )
            for key, value in row.items()
        }
        writer.writerow(csv_row)
    return AuditExportDocument(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        filename=f"{filename_base}.csv",
        row_count=len(rows),
    )


def _resolve_actor_name(event, people: dict[str, object]) -> str:
    return resolve_actor_name(event, people)
