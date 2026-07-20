"""Admin web helpers for native dispatch work orders."""

from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.project import Project
from app.models.subscriber import Subscriber
from app.models.work_order import WorkOrder
from app.schemas.dispatch import (
    WorkOrderAssignmentQueueCreate,
    WorkOrderHeaderCreate,
    WorkOrderHeaderUpdate,
)
from app.schemas.status_presentation import StatusTone
from app.services import dispatch as dispatch_service
from app.services import work_order_views
from app.services.common import coerce_uuid
from app.services.field.work_order_status import WORK_ORDER_TERMINAL_VALUES
from app.services.list_query import ListDefinition, ListFieldDefinition, ListQuery
from app.services.ui_contracts import Action, Kpi, StateValue
from app.services.work_order_views import WorkOrderListFilters

WORK_ORDERS_LIST_URL = "/admin/dispatch/work-orders"

STATUS_OPTIONS = (
    "draft",
    "scheduled",
    "dispatched",
    "in_progress",
    "paused",
    "completed",
    "canceled",
)
PRIORITY_OPTIONS = ("lower", "low", "medium", "normal", "high", "urgent")
WORK_TYPE_OPTIONS = (
    "install",
    "repair",
    "survey",
    "maintenance",
    "disconnect",
    "other",
)
QUEUE_STATUS_OPTIONS = (
    DispatchQueueStatus.queued,
    DispatchQueueStatus.assigned,
    DispatchQueueStatus.skipped,
)


def _subscriber_label(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return "Subscriber"
    full_name = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    return (
        subscriber.company_name
        or full_name
        or subscriber.account_number
        or subscriber.email
        or str(subscriber.id)
    )


def _technician_label(profile: TechnicianProfile) -> str:
    user = profile.system_user
    if user is not None:
        name = (user.display_name or f"{user.first_name} {user.last_name}").strip()
        if name:
            return name
        if user.email:
            return user.email
    metadata = profile.metadata_ or {}
    for key in ("name", "display_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return profile.crm_person_id or str(profile.person_id)


def _parse_dt(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _form_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _work_order_counts(db: Session) -> dict[str, int]:
    rows = (
        db.query(WorkOrder.status, func.count(WorkOrder.id))
        .group_by(WorkOrder.status)
        .all()
    )
    counts = {str(status or "unknown"): int(count) for status, count in rows}
    return {
        "total": sum(counts.values()),
        "active": sum(
            count
            for status, count in counts.items()
            if status not in WORK_ORDER_TERMINAL_VALUES
        ),
        "scheduled": counts.get("scheduled", 0),
        "in_progress": counts.get("in_progress", 0),
        "completed": counts.get("completed", 0),
    }


def _work_order_cohort_url(*, status: str | None = None, active: bool = False) -> str:
    """Drill-down to the queue filtered to exactly the cohort a tile counts.

    The owner supplies this so a summary tile and the rows it summarises can
    never diverge (KPI-parity). The counts are global, so each link narrows by
    the one dimension the tile measures: a single ``status`` for status tiles,
    or ``active=1`` (non-terminal) for the open-work tile.
    """
    params = {"status": status, "active": "1" if active else None}
    query = urlencode({key: value for key, value in params.items() if value})
    return WORK_ORDERS_LIST_URL + (f"?{query}" if query else "")


def _work_order_kpis(counts: dict[str, int]) -> dict[str, Kpi]:
    return {
        "total": Kpi(
            label="Total",
            value=StateValue.present(counts["total"]),
            cohort_url=_work_order_cohort_url(),
        ),
        "active": Kpi(
            label="Active",
            value=StateValue.present(counts["active"]),
            cohort_url=_work_order_cohort_url(active=True),
            tone=StatusTone.info,
        ),
        "scheduled": Kpi(
            label="Scheduled",
            value=StateValue.present(counts["scheduled"]),
            cohort_url=_work_order_cohort_url(status="scheduled"),
            tone=StatusTone.warning,
        ),
        "in_progress": Kpi(
            label="In progress",
            value=StateValue.present(counts["in_progress"]),
            cohort_url=_work_order_cohort_url(status="in_progress"),
            tone=StatusTone.info,
        ),
        "completed": Kpi(
            label="Completed",
            value=StateValue.present(counts["completed"]),
            cohort_url=_work_order_cohort_url(status="completed"),
            tone=StatusTone.positive,
        ),
    }


def _queue_action(work_order: WorkOrder) -> Action:
    """Assignment eligibility owned by the work-order transition command.

    Mirrors ``work_order_commands.preview_assignment``: a soft-deleted or
    terminal work order cannot be assigned. Eligibility is derived here, never
    re-read from the status string in the template.
    """
    if not work_order.is_active:
        allowed, reason = False, "Work order is inactive"
    elif work_order.status in WORK_ORDER_TERMINAL_VALUES:
        allowed = False
        reason = f"Cannot assign a work order in status {work_order.status}"
    else:
        allowed, reason = True, None
    return Action(
        key="queue",
        label="Queue",
        allowed=allowed,
        reason=reason,
        permission="operations:dispatch:assign",
        tone=StatusTone.info,
    )


def _queue_status_by_work_order(db: Session, rows: list[WorkOrder]) -> dict[str, str]:
    ids = [row.id for row in rows]
    if not ids:
        return {}
    queue_rows = (
        db.query(WorkOrderAssignmentQueue)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id.in_(ids))
        .order_by(WorkOrderAssignmentQueue.updated_at.desc())
        .all()
    )
    statuses: dict[str, str] = {}
    for queue in queue_rows:
        key = str(queue.work_order_mirror_id)
        statuses.setdefault(key, queue.status)
    return statuses


def _subscriber_options(db: Session, *, limit: int = 100) -> list[dict[str, str]]:
    rows = (
        db.query(Subscriber).order_by(Subscriber.created_at.desc()).limit(limit).all()
    )
    return [{"id": str(row.id), "label": _subscriber_label(row)} for row in rows]


def _technician_options(db: Session) -> list[dict[str, str]]:
    rows = (
        db.query(TechnicianProfile)
        .filter(TechnicianProfile.is_active.is_(True))
        .order_by(TechnicianProfile.created_at.desc())
        .all()
    )
    return [{"id": str(row.id), "label": _technician_label(row)} for row in rows]


def _project_options(db: Session, *, limit: int = 200) -> list[dict[str, str]]:
    rows = (
        db.query(Project)
        .filter(Project.is_active.is_(True))
        .order_by(Project.updated_at.desc(), Project.id.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(row.id),
            "label": row.name or row.code or row.number or str(row.id),
        }
        for row in rows
    ]


WORK_ORDER_LIST_DEFINITION = ListDefinition(
    key="work_orders",
    fields=(
        ListFieldDefinition("id", "Work order", searchable=True),
        ListFieldDefinition("title", "Title", searchable=True),
        ListFieldDefinition("address", "Address", searchable=True),
        ListFieldDefinition("status", "Status", filterable=True, sortable=True),
        ListFieldDefinition("work_type", "Type", filterable=True),
        ListFieldDefinition("priority", "Priority", filterable=True, sortable=True),
        ListFieldDefinition("scheduled_start", "Scheduled", sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)


def build_work_order_list_query(
    *,
    search: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalize the admin work-order list through its declared capabilities.

    The route submits raw request values; this owner (ui.work_order_list_projection)
    resolves the searchable/filterable fields, sort, and pagination once so no route
    reconstructs those rules. The read itself is delegated to work_order_views.
    """
    normalized_status = status if status in STATUS_OPTIONS else None
    effective_per_page = per_page or WORK_ORDER_LIST_DEFINITION.default_per_page
    if effective_per_page not in WORK_ORDER_LIST_DEFINITION.per_page_options:
        effective_per_page = WORK_ORDER_LIST_DEFINITION.default_per_page
    return WORK_ORDER_LIST_DEFINITION.build_query(
        search=search,
        filters={"status": normalized_status},
        page=max(1, page),
        per_page=effective_per_page,
    )


def list_page(
    db: Session,
    *,
    status: str | None = None,
    q: str | None = None,
    active: bool | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, Any]:
    list_query = build_work_order_list_query(
        search=q, status=status, page=page, per_page=per_page
    )
    filters = WorkOrderListFilters(
        status=list_query.filter_value("status"),
        q=list_query.search,
        is_active=None,
        active=active,
        limit=list_query.per_page,
        offset=list_query.offset,
    )
    rows, total = work_order_views.query_work_orders(
        db,
        filters,
        sort_by=list_query.sort_by,
        sort_dir=list_query.sort_dir,
    )
    queue_status = _queue_status_by_work_order(db, [row for row, _ in rows])
    project_options = _project_options(db)
    project_labels = {item["id"]: item["label"] for item in project_options}
    items = [
        {
            "work_order": row,
            "subscriber": subscriber,
            "subscriber_label": _subscriber_label(subscriber),
            "project_label": project_labels.get(str(row.project_id)),
            "queue_status": queue_status.get(str(row.id)),
            "actions": {"queue": _queue_action(row)},
        }
        for row, subscriber in rows
    ]
    counts = _work_order_counts(db)
    return {
        "items": items,
        "counts": counts,
        "kpis": _work_order_kpis(counts),
        "status_filter": list_query.filter_value("status"),
        "active_filter": bool(active),
        "q": list_query.search or "",
        "page": list_query.page,
        "per_page": list_query.per_page,
        "total": total,
        "total_pages": max(1, ceil(total / list_query.per_page)) if total else 1,
        "statuses": STATUS_OPTIONS,
        "priorities": PRIORITY_OPTIONS,
        "work_types": WORK_TYPE_OPTIONS,
        "queue_statuses": QUEUE_STATUS_OPTIONS,
        "subscriber_options": _subscriber_options(db),
        "project_options": project_options,
        "technician_options": _technician_options(db),
    }


def create_from_form(
    db: Session,
    form: dict[str, Any],
    *,
    auth: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> WorkOrder:
    payload = WorkOrderHeaderCreate(
        public_id=_clean(form.get("public_id")),
        subscriber_id=coerce_uuid(str(form.get("subscriber_id"))),
        project_id=(
            coerce_uuid(str(form["project_id"]))
            if str(form.get("project_id") or "").strip()
            else None
        ),
        requires_as_built_evidence=(
            _form_bool(form.get("requires_as_built_evidence"), default=True)
        ),
        title=str(form.get("title") or "").strip(),
        status=str(form.get("status") or "scheduled"),
        priority=_clean(form.get("priority")) or "normal",
        work_type=_clean(form.get("work_type")) or "install",
        description=_clean(form.get("description")),
        address=_clean(form.get("address")),
        scheduled_start=_parse_dt(form.get("scheduled_start")),
        scheduled_end=_parse_dt(form.get("scheduled_end")),
        estimated_duration_minutes=(
            int(form["estimated_duration_minutes"])
            if str(form.get("estimated_duration_minutes") or "").strip()
            else None
        ),
        required_skills=_split_csv(form.get("required_skills")),
        tags=_split_csv(form.get("tags")),
        access_notes=_clean(form.get("access_notes")),
    )
    return dispatch_service.work_order_headers.create(
        db,
        payload,
        auth=auth,
        request_id=request_id,
    )


def update_from_form(
    db: Session,
    work_order_id: str,
    form: dict[str, Any],
    *,
    auth: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> WorkOrder:
    values: dict[str, Any] = {}
    if "requires_as_built_evidence" in form:
        values["requires_as_built_evidence"] = _form_bool(
            form.get("requires_as_built_evidence"), default=True
        )
    if str(form.get("project_id") or "").strip():
        values["project_id"] = coerce_uuid(str(form["project_id"]))
    payload = WorkOrderHeaderUpdate(
        **values,
        title=_clean(form.get("title")),
        priority=_clean(form.get("priority")),
        work_type=_clean(form.get("work_type")),
        address=_clean(form.get("address")),
        scheduled_start=_parse_dt(form.get("scheduled_start")),
        scheduled_end=_parse_dt(form.get("scheduled_end")),
        estimated_arrival_at=_parse_dt(form.get("estimated_arrival_at")),
        estimated_duration_minutes=(
            int(form["estimated_duration_minutes"])
            if str(form.get("estimated_duration_minutes") or "").strip()
            else None
        ),
        required_skills=_split_csv(form.get("required_skills")),
        tags=_split_csv(form.get("tags")),
        access_notes=_clean(form.get("access_notes")),
    )
    return dispatch_service.work_order_headers.update(
        db,
        work_order_id,
        payload,
        auth=auth,
        request_id=request_id,
    )


def queue_assignment_from_form(
    db: Session,
    work_order_id: str,
    form: dict[str, Any],
    *,
    auth: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> WorkOrderAssignmentQueue:
    technician_id = _clean(form.get("assigned_technician_id"))
    if technician_id is None:
        raise HTTPException(status_code=422, detail="Technician is required")
    payload = WorkOrderAssignmentQueueCreate(
        crm_work_order_id=work_order_id,
        status=_clean(form.get("status")) or DispatchQueueStatus.queued,
        assigned_technician_id=coerce_uuid(technician_id),
        reason=_clean(form.get("reason")),
    )
    return dispatch_service.assignment_queue.create(
        db,
        payload,
        auth=auth,
        request_id=request_id,
    )
