"""Admin web helpers for native dispatch work orders."""

from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from app.schemas.dispatch import (
    WorkOrderAssignmentQueueCreate,
    WorkOrderHeaderCreate,
    WorkOrderHeaderUpdate,
)
from app.services import dispatch as dispatch_service
from app.services.common import coerce_uuid
from app.services.field.work_order_status import WORK_ORDER_TERMINAL_VALUES

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


def _work_order_counts(db: Session) -> dict[str, int]:
    rows = (
        db.query(WorkOrderMirror.status, func.count(WorkOrderMirror.id))
        .group_by(WorkOrderMirror.status)
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


def _queue_status_by_work_order(
    db: Session, rows: list[WorkOrderMirror]
) -> dict[str, str]:
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


def list_page(
    db: Session,
    *,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, Any]:
    page = max(1, page)
    per_page = max(10, min(100, per_page))
    status_filter = status if status in STATUS_OPTIONS else None
    query = db.query(WorkOrderMirror, Subscriber).join(
        Subscriber, Subscriber.id == WorkOrderMirror.subscriber_id
    )
    if status_filter:
        query = query.filter(WorkOrderMirror.status == status_filter)
    search = (q or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                WorkOrderMirror.crm_work_order_id.ilike(like),
                WorkOrderMirror.title.ilike(like),
                WorkOrderMirror.address.ilike(like),
                Subscriber.first_name.ilike(like),
                Subscriber.last_name.ilike(like),
                Subscriber.email.ilike(like),
                Subscriber.account_number.ilike(like),
            )
        )

    total = query.count()
    rows = (
        query.order_by(WorkOrderMirror.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    work_orders = [row for row, _subscriber in rows]
    queue_status = _queue_status_by_work_order(db, work_orders)
    items = [
        {
            "work_order": row,
            "subscriber": subscriber,
            "subscriber_label": _subscriber_label(subscriber),
            "queue_status": queue_status.get(str(row.id)),
        }
        for row, subscriber in rows
    ]
    return {
        "items": items,
        "counts": _work_order_counts(db),
        "status_filter": status_filter,
        "q": search,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, ceil(total / per_page)) if total else 1,
        "statuses": STATUS_OPTIONS,
        "priorities": PRIORITY_OPTIONS,
        "work_types": WORK_TYPE_OPTIONS,
        "queue_statuses": QUEUE_STATUS_OPTIONS,
        "subscriber_options": _subscriber_options(db),
        "technician_options": _technician_options(db),
    }


def create_from_form(db: Session, form: dict[str, Any]) -> WorkOrderMirror:
    payload = WorkOrderHeaderCreate(
        public_id=_clean(form.get("public_id")),
        subscriber_id=coerce_uuid(str(form.get("subscriber_id"))),
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
    return dispatch_service.work_order_headers.create(db, payload)


def update_from_form(
    db: Session, work_order_id: str, form: dict[str, Any]
) -> WorkOrderMirror:
    payload = WorkOrderHeaderUpdate(
        title=_clean(form.get("title")),
        status=_clean(form.get("status")),
        priority=_clean(form.get("priority")),
        work_type=_clean(form.get("work_type")),
        assigned_to_name=_clean(form.get("assigned_to_name")),
        technician_name=_clean(form.get("technician_name")),
        technician_phone=_clean(form.get("technician_phone")),
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
    return dispatch_service.work_order_headers.update(db, work_order_id, payload)


def queue_assignment_from_form(
    db: Session, work_order_id: str, form: dict[str, Any]
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
    return dispatch_service.assignment_queue.create(db, payload)
