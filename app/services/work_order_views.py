"""Native read views over CRM-sourced work-order mirror rows.

Phase 2 keeps CRM as the work-order source of truth while sub absorbs the
field-ops surface. This module is the in-process read layer later admin,
dispatch, and field APIs can use without fanning out to CRM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from app.services.common import coerce_uuid

TERMINAL_STATUSES = frozenset({"completed", "canceled", "cancelled"})
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class WorkOrderListFilters:
    status: str | None = None
    priority: str | None = None
    work_type: str | None = None
    subscriber_id: str | None = None
    crm_ticket_id: str | None = None
    crm_project_id: str | None = None
    assigned_to_crm_person_id: str | None = None
    q: str | None = None
    is_active: bool | None = True
    scheduled_from: datetime | None = None
    scheduled_to: datetime | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _subscriber_name(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    full = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    return subscriber.company_name or full or subscriber.email or subscriber.account_number


def _subscriber_snapshot(row: WorkOrderMirror, subscriber: Subscriber | None) -> dict:
    return {
        "account_id": str(row.subscriber_id),
        "account_name": _subscriber_name(subscriber),
        "account_number": subscriber.account_number if subscriber else None,
        "account_email": subscriber.email if subscriber else None,
        "account_phone": subscriber.phone if subscriber else None,
    }


def row_to_item(
    row: WorkOrderMirror,
    *,
    subscriber: Subscriber | None = None,
    include_internal: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": row.crm_work_order_id,
        "title": row.title,
        "status": row.status,
        "work_type": row.work_type,
        "priority": row.priority,
        "technician_name": row.technician_name or row.assigned_to_name,
        "technician_phone": row.technician_phone,
        "address": row.address,
        "scheduled_start": _dt(row.scheduled_start),
        "scheduled_end": _dt(row.scheduled_end),
        "estimated_arrival_at": _dt(row.estimated_arrival_at),
        "estimated_duration_minutes": row.estimated_duration_minutes,
        "started_at": _dt(row.started_at),
        "paused_at": _dt(row.paused_at),
        "resumed_at": _dt(row.resumed_at),
        "completed_at": _dt(row.completed_at),
        "total_active_seconds": row.total_active_seconds,
        "created_at": _dt(row.work_order_created_at),
    }
    if include_internal:
        item.update(
            {
                "description": row.description,
                "crm_ticket_id": row.crm_ticket_id,
                "crm_project_id": row.crm_project_id,
                "assigned_to_crm_person_id": row.assigned_to_crm_person_id,
                "assigned_to_name": row.assigned_to_name,
                "required_skills": row.required_skills or [],
                "tags": row.tags or [],
                "access_notes": row.access_notes,
                "is_active": row.is_active,
                "metadata": row.metadata_ or {},
            }
        )
    if subscriber is not None:
        item.update(_subscriber_snapshot(row, subscriber))
    return item


def _base_query(db: Session):
    return (
        db.query(WorkOrderMirror, Subscriber)
        .join(Subscriber, Subscriber.id == WorkOrderMirror.subscriber_id)
    )


def _apply_filters(query, filters: WorkOrderListFilters):
    if filters.is_active is not None:
        query = query.filter(WorkOrderMirror.is_active.is_(filters.is_active))
    if filters.status:
        query = query.filter(WorkOrderMirror.status == filters.status)
    if filters.priority:
        query = query.filter(WorkOrderMirror.priority == filters.priority)
    if filters.work_type:
        query = query.filter(WorkOrderMirror.work_type == filters.work_type)
    if filters.subscriber_id:
        query = query.filter(
            WorkOrderMirror.subscriber_id == coerce_uuid(filters.subscriber_id)
        )
    if filters.crm_ticket_id:
        query = query.filter(WorkOrderMirror.crm_ticket_id == filters.crm_ticket_id)
    if filters.crm_project_id:
        query = query.filter(WorkOrderMirror.crm_project_id == filters.crm_project_id)
    if filters.assigned_to_crm_person_id:
        query = query.filter(
            WorkOrderMirror.assigned_to_crm_person_id
            == filters.assigned_to_crm_person_id
        )
    if filters.scheduled_from:
        query = query.filter(WorkOrderMirror.scheduled_start >= filters.scheduled_from)
    if filters.scheduled_to:
        query = query.filter(WorkOrderMirror.scheduled_start <= filters.scheduled_to)
    q = (filters.q or "").strip()
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                WorkOrderMirror.title.ilike(pattern),
                WorkOrderMirror.address.ilike(pattern),
                WorkOrderMirror.crm_work_order_id.ilike(pattern),
                Subscriber.first_name.ilike(pattern),
                Subscriber.last_name.ilike(pattern),
                Subscriber.company_name.ilike(pattern),
                Subscriber.email.ilike(pattern),
                Subscriber.account_number.ilike(pattern),
            )
        )
    return query


def list_work_orders(db: Session, filters: WorkOrderListFilters | None = None) -> dict:
    filters = filters or WorkOrderListFilters()
    limit = min(max(int(filters.limit or DEFAULT_LIMIT), 1), MAX_LIMIT)
    offset = max(int(filters.offset or 0), 0)

    filtered = _apply_filters(_base_query(db), filters)
    total = int(
        filtered.with_entities(func.count(WorkOrderMirror.id)).scalar() or 0
    )
    rows = (
        filtered.order_by(
            WorkOrderMirror.scheduled_start.asc().nullslast(),
            WorkOrderMirror.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
        .all()
    )
    items = [
        row_to_item(row, subscriber=subscriber, include_internal=True)
        for row, subscriber in rows
    ]
    return {
        "work_orders": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": summary(db, filters),
    }


def get_work_order(db: Session, crm_work_order_id: str) -> dict | None:
    pair = (
        _base_query(db)
        .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
        .first()
    )
    if pair is None:
        return None
    row, subscriber = pair
    return row_to_item(row, subscriber=subscriber, include_internal=True)


def summary(db: Session, filters: WorkOrderListFilters | None = None) -> dict:
    filters = filters or WorkOrderListFilters()
    query = _apply_filters(_base_query(db), filters)
    rows = query.with_entities(WorkOrderMirror.status, func.count(WorkOrderMirror.id)).group_by(
        WorkOrderMirror.status
    )
    by_status = {str(status): int(count) for status, count in rows}
    total = sum(by_status.values())
    terminal = sum(count for status, count in by_status.items() if status in TERMINAL_STATUSES)
    open_count = total - terminal
    now = datetime.now(UTC)
    overdue = int(
        _apply_filters(_base_query(db), filters)
        .filter(WorkOrderMirror.scheduled_start < now)
        .filter(WorkOrderMirror.status.notin_(TERMINAL_STATUSES))
        .with_entities(func.count(WorkOrderMirror.id))
        .scalar()
        or 0
    )
    return {
        "total": total,
        "open": open_count,
        "terminal": terminal,
        "overdue": overdue,
        "by_status": by_status,
    }


def options(db: Session) -> dict[str, list[str]]:
    def values(column) -> list[str]:
        return [
            str(value)
            for value in db.scalars(
                select(column).where(column.isnot(None)).distinct().order_by(column)
            ).all()
            if value
        ]

    return {
        "statuses": values(WorkOrderMirror.status),
        "priorities": values(WorkOrderMirror.priority),
        "work_types": values(WorkOrderMirror.work_type),
    }
