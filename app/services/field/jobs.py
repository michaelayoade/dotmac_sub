"""Technician-scoped field job reads over CRM-sourced work-order mirrors.

Phase 2 keeps CRM as the source of truth for work-order mutations. This module
gives the field app a native sub read surface while the CRM webhook/reconcile
pipeline continues to hydrate ``work_order_mirror``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.schemas.field import (
    FieldCustomer,
    FieldJobDetail,
    FieldJobLocation,
    FieldJobSummary,
    FieldMeResponse,
)
from app.services.common import apply_pagination, coerce_uuid

TERMINAL_STATUSES = frozenset({"completed", "canceled", "cancelled"})
OPEN_STATUSES = frozenset({"scheduled", "dispatched", "in_progress", "paused"})


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _technician_name(profile: TechnicianProfile, user: SystemUser | None) -> str:
    if user is not None:
        display_name = (
            user.display_name or f"{user.first_name} {user.last_name}".strip()
        )
        if display_name:
            return display_name
    metadata = profile.metadata_ or {}
    for key in ("name", "display_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return profile.crm_person_id or str(profile.person_id)


def _system_user(db: Session, profile: TechnicianProfile) -> SystemUser | None:
    if profile.system_user_id is None:
        return None
    return db.get(SystemUser, profile.system_user_id)


def _profile_from_principal(
    db: Session, principal: dict[str, Any]
) -> TechnicianProfile:
    ids = {
        str(value)
        for value in (
            principal.get("principal_id"),
            principal.get("person_id"),
            principal.get("subscriber_id"),
        )
        if value
    }
    uuid_ids: list[UUID] = []
    for value in ids:
        try:
            uuid_ids.append(coerce_uuid(value))
        except ValueError:
            continue

    query = db.query(TechnicianProfile).filter(TechnicianProfile.is_active.is_(True))
    if uuid_ids:
        profile = query.filter(
            or_(
                TechnicianProfile.system_user_id.in_(uuid_ids),
                TechnicianProfile.person_id.in_(uuid_ids),
            )
        ).first()
        if profile is not None:
            return profile

    crm_person_id = principal.get("crm_person_id")
    if crm_person_id:
        profile = query.filter(
            TechnicianProfile.crm_person_id == str(crm_person_id)
        ).first()
        if profile is not None:
            return profile

    raise HTTPException(status_code=404, detail="Technician profile not found")


def _scoped_query(db: Session, profile: TechnicianProfile):
    assignment_ids = select(WorkOrderAssignmentQueue.work_order_mirror_id).filter(
        WorkOrderAssignmentQueue.assigned_technician_id == profile.id
    )
    query = db.query(WorkOrderMirror).filter(WorkOrderMirror.is_active.is_(True))
    clauses = [WorkOrderMirror.id.in_(assignment_ids)]
    if profile.crm_person_id:
        clauses.append(
            WorkOrderMirror.assigned_to_crm_person_id == profile.crm_person_id
        )
    return query.filter(or_(*clauses))


def _subscriber_name(subscriber: Subscriber) -> str | None:
    full = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    return (
        subscriber.company_name or full or subscriber.email or subscriber.account_number
    )


def _customer(
    row: WorkOrderMirror, subscriber: Subscriber | None
) -> FieldCustomer | None:
    if subscriber is None:
        return None
    status = getattr(subscriber, "status", None)
    return FieldCustomer(
        subscriber_id=subscriber.id,
        name=_subscriber_name(subscriber),
        phone=subscriber.phone,
        email=subscriber.email,
        address_text=row.address,
        service_plan=getattr(subscriber, "service_plan", None),
        account_number=subscriber.account_number,
        status=(getattr(status, "value", None) or str(status)) if status else None,
    )


def _location(row: WorkOrderMirror) -> FieldJobLocation:
    metadata = row.metadata_ or {}
    latitude = metadata.get("latitude") or metadata.get("lat")
    longitude = metadata.get("longitude") or metadata.get("lng")
    if isinstance(metadata.get("location"), dict):
        location = metadata["location"]
        latitude = latitude or location.get("latitude") or location.get("lat")
        longitude = longitude or location.get("longitude") or location.get("lng")
    parsed_latitude = latitude if isinstance(latitude, int | float) else None
    parsed_longitude = longitude if isinstance(longitude, int | float) else None
    return FieldJobLocation(
        latitude=parsed_latitude,
        longitude=parsed_longitude,
        address_text=row.address,
        source="cached"
        if parsed_latitude is not None and parsed_longitude is not None
        else "address_only",
    )


def _summary(row: WorkOrderMirror) -> FieldJobSummary:
    return FieldJobSummary(
        id=row.crm_work_order_id,
        work_order_mirror_id=row.id,
        title=row.title,
        description=row.description,
        status=row.status,
        priority=row.priority,
        work_type=row.work_type,
        scheduled_start=row.scheduled_start,
        scheduled_end=row.scheduled_end,
        estimated_duration_minutes=row.estimated_duration_minutes,
        estimated_arrival_at=row.estimated_arrival_at,
        started_at=row.started_at,
        paused_at=row.paused_at,
        resumed_at=row.resumed_at,
        completed_at=row.completed_at,
        total_active_seconds=row.total_active_seconds,
        technician_name=row.technician_name or row.assigned_to_name,
        technician_phone=row.technician_phone,
        address=row.address,
        tags=_string_list(row.tags),
    )


class FieldJobs:
    @staticmethod
    def me(db: Session, principal: dict[str, Any]) -> FieldMeResponse:
        profile = _profile_from_principal(db, principal)
        user = _system_user(db, profile)
        today = datetime.now(UTC).date()
        scoped = _scoped_query(db, profile)
        open_jobs = [
            row
            for row in scoped.filter(WorkOrderMirror.status.in_(OPEN_STATUSES)).all()
            if row.scheduled_start is None or row.scheduled_start.date() <= today
        ]
        completed_today = [
            row
            for row in scoped.filter(WorkOrderMirror.status == "completed")
            .filter(WorkOrderMirror.completed_at.isnot(None))
            .all()
            if row.completed_at and row.completed_at.date() == today
        ]
        return FieldMeResponse(
            person_id=profile.person_id,
            name=_technician_name(profile, user),
            email=user.email if user else None,
            technician_title=profile.title,
            region=profile.region,
            open_jobs=len(open_jobs),
            completed_today=len(completed_today),
        )

    @staticmethod
    def list(
        db: Session,
        principal: dict[str, Any],
        *,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FieldJobSummary]:
        profile = _profile_from_principal(db, principal)
        query = _scoped_query(db, profile)
        if status:
            query = query.filter(WorkOrderMirror.status == status)
        if date_from:
            query = query.filter(
                or_(
                    WorkOrderMirror.scheduled_start.is_(None),
                    WorkOrderMirror.scheduled_start >= date_from,
                )
            )
        if date_to:
            query = query.filter(
                or_(
                    WorkOrderMirror.scheduled_start.is_(None),
                    WorkOrderMirror.scheduled_start <= date_to,
                )
            )
        query = query.order_by(
            WorkOrderMirror.scheduled_start.asc().nullslast(),
            WorkOrderMirror.created_at.asc(),
        )
        return [_summary(row) for row in apply_pagination(query, limit, offset).all()]

    @staticmethod
    def get_detail(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> FieldJobDetail:
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        subscriber = db.get(Subscriber, row.subscriber_id)
        return FieldJobDetail(
            job=_summary(row),
            customer=_customer(row, subscriber),
            location=_location(row),
            ticket_ref=row.crm_ticket_id,
            project_id=row.crm_project_id,
            access_notes=row.access_notes,
            history=[],
        )


field_jobs = FieldJobs()
