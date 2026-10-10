"""Canonical authenticated execution identity and current assignment scope."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Query, Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.domain_errors import DomainError


class FieldActorKind(StrEnum):
    technician = "technician"
    vendor = "vendor"


@dataclass(frozen=True, slots=True)
class ResolveFieldActor:
    system_user_id: UUID
    lock: bool = False


@dataclass(frozen=True, slots=True)
class FieldActor:
    kind: FieldActorKind
    system_user_id: UUID
    technician_id: UUID | None = None
    vendor_user_id: UUID | None = None
    native_vendor_id: UUID | None = None
    person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FieldWorkOrderScope:
    actor: FieldActor
    public_id: str
    lock: bool = False


class FieldAccessError(DomainError):
    pass


def _denied(message: str) -> FieldAccessError:
    return FieldAccessError(
        code="operations.field_work_order_access.denied",
        message=message,
        retryable=False,
    )


def resolve_field_actor(db: Session, query: ResolveFieldActor) -> FieldActor:
    users = (
        db.query(SystemUser)
        .populate_existing()
        .filter(SystemUser.id == query.system_user_id)
    )
    user = (users.with_for_update() if query.lock else users).one_or_none()
    if user is None or not user.is_active:
        raise _denied("Active field user not found")
    # Any vendor membership prevents fallback into the staff plane, including
    # disabled membership and a stale technician profile on the same user.
    membership_query = (
        db.query(FieldVendorUser)
        .populate_existing()
        .filter(FieldVendorUser.system_user_id == user.id)
    )
    memberships = (
        membership_query.with_for_update() if query.lock else membership_query
    ).all()
    if user.user_type == UserType.vendor and not memberships:
        raise _denied("Vendor membership is unavailable")
    if memberships:
        # Historical memberships never participate in execution or locking.
        # More than one active membership is ambiguous even when one vendor
        # is disabled: enabling that vendor must not silently change scope.
        active = [item for item in memberships if item.is_active]
        if len(active) != 1:
            raise _denied("Vendor membership is unavailable or ambiguous")
        membership = active[0]
        db.refresh(membership.vendor)
        native_id = membership.vendor.native_vendor_id
        if native_id is None:
            raise _denied("Native vendor link is unavailable")
        vendors = db.query(Vendor).populate_existing().filter(Vendor.id == native_id)
        native = (vendors.with_for_update() if query.lock else vendors).one_or_none()
        if native is None or not native.is_active:
            raise _denied("Native vendor link is unavailable")
        # Import/admin writers acquire native vendor before its portal bridge.
        # Follow that same order, then revalidate the bridge after waiting.
        bridges = (
            db.query(FieldVendor)
            .populate_existing()
            .filter(FieldVendor.id == membership.vendor_id)
        )
        bridge = (bridges.with_for_update() if query.lock else bridges).one_or_none()
        if (
            bridge is None
            or not bridge.is_active
            or bridge.native_vendor_id != native.id
        ):
            raise _denied("Native vendor link is unavailable")
        return FieldActor(
            kind=FieldActorKind.vendor,
            system_user_id=user.id,
            vendor_user_id=membership.id,
            native_vendor_id=native.id,
        )
    profiles = (
        db.query(TechnicianProfile)
        .populate_existing()
        .filter(
            TechnicianProfile.is_active.is_(True),
            or_(
                TechnicianProfile.system_user_id == user.id,
                TechnicianProfile.person_id == user.id,
            ),
        )
        .all()
    )
    if len(profiles) != 1:
        raise _denied("Technician profile is unavailable or ambiguous")
    profile = profiles[0]
    return FieldActor(
        kind=FieldActorKind.technician,
        system_user_id=user.id,
        technician_id=profile.id,
        person_id=profile.person_id,
    )


def scoped_work_orders(db: Session, actor: FieldActor) -> Query[WorkOrder]:
    assignment = select(WorkOrderAssignmentQueue.work_order_mirror_id).where(
        WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned
    )
    if actor.kind == FieldActorKind.vendor:
        if actor.native_vendor_id is None:
            raise _denied("Native vendor link is unavailable")
        assignment = assignment.where(
            WorkOrderAssignmentQueue.assigned_vendor_id == actor.native_vendor_id
        )
        predicate: ColumnElement[bool] = WorkOrder.id.in_(assignment)
    else:
        if actor.technician_id is None:
            raise _denied("Technician profile is unavailable")
        assignment = assignment.where(
            WorkOrderAssignmentQueue.assigned_technician_id == actor.technician_id
        )
        predicate = WorkOrder.id.in_(assignment)
    return db.query(WorkOrder).filter(WorkOrder.is_active.is_(True), predicate)


def require_work_order(db: Session, query: FieldWorkOrderScope) -> WorkOrder:
    statement = (
        db.query(WorkOrder)
        .populate_existing()
        .filter(WorkOrder.public_id == query.public_id, WorkOrder.is_active.is_(True))
    )
    if query.lock:
        statement = statement.with_for_update()
    row = statement.one_or_none()
    if row is None:
        raise FieldAccessError(
            code="operations.field_work_order_access.not_found",
            message="Job not found",
            retryable=False,
        )
    # Assignment writers share the work-order lock. Refresh identity and scope
    # after that lock, so waiting on a reassignment never grants stale access.
    current = resolve_field_actor(
        db, ResolveFieldActor(query.actor.system_user_id, lock=query.lock)
    )
    if (
        current != query.actor
        or scoped_work_orders(db, current).filter(WorkOrder.id == row.id).one_or_none()
        is None
    ):
        raise FieldAccessError(
            code="operations.field_work_order_access.not_found",
            message="Job not found",
            retryable=False,
        )
    return row
