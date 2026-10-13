"""Customer disclosure of the exact currently assigned, manually reviewed person."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_identity import IdentityState
from app.models.field_vendor import FieldVendorUser
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import field_identity
from app.services.domain_errors import DomainError
from app.services.object_storage import ObjectNotFoundError, StreamResult
from app.services.work_order_assignment_contracts import (
    CurrentWorkOrderAssignmentQuery,
    VendorAssignmentTarget,
)
from app.services.work_order_commands import _vendor_selection, work_order_commands
from app.services.work_order_errors import WorkOrderCommandError


class IdentityUnavailableReason(StrEnum):
    unassigned = "unassigned"
    assignment_generation_unavailable = "assignment_generation_unavailable"
    actual_crew_not_selected = "actual_crew_not_selected"
    identity_unavailable = "identity_unavailable"
    identity_not_approved = "identity_not_approved"


@dataclass(frozen=True, slots=True)
class CustomerFieldIdentityQuery:
    subscriber_id: UUID
    work_order_id: UUID


@dataclass(frozen=True, slots=True)
class CustomerFieldPortraitQuery:
    subscriber_id: UUID
    work_order_id: UUID
    assignment_generation: UUID
    portrait_revision: int


@dataclass(frozen=True, slots=True)
class CustomerFieldIdentityCard:
    display_name: str
    company_name: str
    verification_state: IdentityState
    reviewed_at: datetime
    portrait_revision: int
    assignment_generation: UUID


@dataclass(frozen=True, slots=True)
class CustomerFieldIdentityOutcome:
    work_order_id: UUID
    card: CustomerFieldIdentityCard | None
    unavailable_reason: IdentityUnavailableReason | None


@dataclass(frozen=True, slots=True)
class _AssignedPerson:
    subject_id: UUID | None
    generation: UUID | None
    reason: IdentityUnavailableReason | None


class CustomerFieldIdentityError(DomainError):
    """Stable scope or stale-portrait refusal, without identity disclosure."""


def _error(suffix: str) -> CustomerFieldIdentityError:
    return CustomerFieldIdentityError(
        code=f"customer.field_identity.{suffix}",
        message="Field identity is unavailable for this request",
    )


def _assigned_person(
    db: Session, *, query: CustomerFieldIdentityQuery
) -> _AssignedPerson:
    # Discover candidates without locks. Never acquire a subject lock while
    # holding the visit: field transitions lock SystemUser before WorkOrder.
    order = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == query.work_order_id)
        .execution_options(populate_existing=True)
    )
    if (
        order is None
        or order.subscriber_id != query.subscriber_id
        or not order.is_active
        or order.work_order_kind != "customer"
    ):
        raise _error("not_found")
    assignment = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(work_order_id=order.id)
    )
    subject_id = None
    if (
        assignment is not None
        and assignment.technician_id is not None
        and assignment.vendor_id is None
    ):
        subject_id = db.scalar(
            select(TechnicianProfile.system_user_id).where(
                TechnicianProfile.id == assignment.technician_id
            )
        )
    if assignment is not None and assignment.vendor_user_id is not None:
        subject_id = db.scalar(
            select(FieldVendorUser.system_user_id).where(
                FieldVendorUser.id == assignment.vendor_user_id
            )
        )
    subject = None
    technician = None
    if subject_id is not None:
        assert assignment is not None
        # Refresh after each wait: identity-map snapshots are not admission.
        # Stable lock order is subject -> profile -> native visit; once the
        # visit is locked, a changed candidate fails closed without new locks.
        subject = db.scalar(
            select(SystemUser)
            .where(SystemUser.id == subject_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        technician = db.scalar(
            select(TechnicianProfile)
            .where(TechnicianProfile.id == assignment.technician_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    order = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == query.work_order_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        order is None
        or order.subscriber_id != query.subscriber_id
        or not order.is_active
        or order.work_order_kind != "customer"
    ):
        raise _error("not_found")
    current = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(work_order_id=order.id)
    )
    if current != assignment:
        return _AssignedPerson(
            None, None, IdentityUnavailableReason.identity_unavailable
        )
    if assignment is None:
        return _AssignedPerson(None, None, IdentityUnavailableReason.unassigned)
    if assignment.vendor_id is not None:
        if assignment.vendor_user_id is None:
            return _AssignedPerson(
                None,
                assignment.generation,
                IdentityUnavailableReason.actual_crew_not_selected,
            )
        if subject is None or not subject.is_active or assignment.generation is None:
            return _AssignedPerson(
                None,
                assignment.generation,
                IdentityUnavailableReason.identity_unavailable,
            )
        try:
            member = _vendor_selection(
                db,
                VendorAssignmentTarget(assignment.vendor_id, assignment.vendor_user_id),
                lock=True,
            )
        except WorkOrderCommandError:
            return _AssignedPerson(
                None,
                assignment.generation,
                IdentityUnavailableReason.identity_unavailable,
            )
        if member.system_user_id != subject.id:
            return _AssignedPerson(
                None,
                assignment.generation,
                IdentityUnavailableReason.identity_unavailable,
            )
        return _AssignedPerson(subject.id, assignment.generation, None)
    if assignment.technician_id is None:
        return _AssignedPerson(
            None, assignment.generation, IdentityUnavailableReason.identity_unavailable
        )
    if assignment.generation is None:
        return _AssignedPerson(
            None, None, IdentityUnavailableReason.assignment_generation_unavailable
        )
    if (
        technician is None
        or not technician.is_active
        or technician.system_user_id != subject_id
    ):
        return _AssignedPerson(
            None, assignment.generation, IdentityUnavailableReason.identity_unavailable
        )
    # Only the explicit native SystemUser binding identifies the assigned person.
    if subject is None or not subject.is_active:
        return _AssignedPerson(
            None, assignment.generation, IdentityUnavailableReason.identity_unavailable
        )
    return _AssignedPerson(subject.id, assignment.generation, None)


def card(
    db: Session, *, query: CustomerFieldIdentityQuery
) -> CustomerFieldIdentityOutcome:
    assigned = _assigned_person(db, query=query)
    if (
        assigned.reason is not None
        or assigned.subject_id is None
        or assigned.generation is None
    ):
        return CustomerFieldIdentityOutcome(query.work_order_id, None, assigned.reason)
    approved = field_identity.approved_customer_card(db, subject_id=assigned.subject_id)
    if approved is None:
        return CustomerFieldIdentityOutcome(
            query.work_order_id, None, IdentityUnavailableReason.identity_not_approved
        )
    return CustomerFieldIdentityOutcome(
        query.work_order_id,
        CustomerFieldIdentityCard(
            approved.display_name,
            approved.company_name,
            approved.verification_state,
            approved.reviewed_at,
            approved.revision,
            assigned.generation,
        ),
        None,
    )


def portrait(db: Session, *, query: CustomerFieldPortraitQuery) -> StreamResult:
    assigned = _assigned_person(
        db, query=CustomerFieldIdentityQuery(query.subscriber_id, query.work_order_id)
    )
    if (
        assigned.reason is not None
        or assigned.subject_id is None
        or assigned.generation != query.assignment_generation
        or query.portrait_revision < 1
    ):
        raise _error("denied")
    try:
        return field_identity.approved_portrait_stream(
            db, subject_id=assigned.subject_id, revision=query.portrait_revision
        )
    except field_identity.FieldIdentityError as exc:
        raise _error("denied") from exc
    except ObjectNotFoundError as exc:
        raise _error("not_found") from exc
