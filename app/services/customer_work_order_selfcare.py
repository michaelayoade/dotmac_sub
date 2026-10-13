"""Native customer actions for Sub-owned field-service work orders.

This service owns customer-visible technician location reads and the single
customer feedback write. It never calls an external work system and scopes
every operation to the subscriber before returning data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispatch import (
    TechnicianProfile,
)
from app.models.field_job_event import FieldJobEvent
from app.models.field_location import FieldTechLocationPing, FieldTechPresence
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.portal import TechnicianLocation
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.work_order_assignment_contracts import (
    CurrentWorkOrderAssignment,
    CurrentWorkOrderAssignmentQuery,
)
from app.services.work_order_commands import work_order_commands

_TRACKABLE_STATUSES = frozenset({"dispatched", "in_progress", "paused"})
_RATABLE_STATUSES = frozenset({"completed"})
TECHNICIAN_LOCATION_FRESHNESS = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class TechnicianLocationQuery:
    subscriber_id: UUID | None
    work_order_public_id: str


@dataclass(frozen=True, slots=True)
class CustomerTrackingEligibility:
    available: bool
    reason: str | None = None
    assignment: CurrentWorkOrderAssignment | None = None


def _owned_location_work_order(
    db: Session, query: TechnicianLocationQuery
) -> WorkOrder | None:
    if query.subscriber_id is None:
        return None
    return db.scalar(
        select(WorkOrder)
        .where(
            WorkOrder.subscriber_id == query.subscriber_id,
            WorkOrder.public_id == query.work_order_public_id,
            WorkOrder.is_active.is_(True),
        )
        .with_for_update(read=True)
    )


def tracking_eligibility(
    db: Session, *, query: TechnicianLocationQuery
) -> CustomerTrackingEligibility:
    """Actual departure on the current assignment grants tracking until terminal."""
    row = _owned_location_work_order(db, query)
    if row is None:
        return CustomerTrackingEligibility(False, "not_found")
    if row.status not in _TRACKABLE_STATUSES or row.completed_at is not None:
        return CustomerTrackingEligibility(False, "not_active")
    assignment = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(row.id)
    )
    if assignment is None or assignment.technician_id is None:
        return CustomerTrackingEligibility(False, "not_assigned")
    profile = db.get(
        TechnicianProfile, assignment.technician_id, populate_existing=True
    )
    if profile is None or not profile.is_active:
        return CustomerTrackingEligibility(False, "not_assigned")
    departure = db.scalar(
        select(FieldJobEvent.id)
        .where(
            FieldJobEvent.work_order_mirror_id == row.id,
            FieldJobEvent.author_technician_id == assignment.technician_id,
            FieldJobEvent.event == "en_route",
            FieldJobEvent.assignment_generation == assignment.generation,
        )
        .limit(1)
    )
    if departure is None:
        return CustomerTrackingEligibility(False, "not_departed")
    return CustomerTrackingEligibility(True, assignment=assignment)


def technician_location(
    db: Session, *, query: TechnicianLocationQuery
) -> TechnicianLocation:
    """Read a fresh same-job fix bound to the current departed assignment."""
    eligibility = tracking_eligibility(db, query=query)
    if not eligibility.available or eligibility.assignment is None:
        return TechnicianLocation(
            available=False,
            reason=eligibility.reason,
            work_order_id=query.work_order_public_id
            if eligibility.reason != "not_found"
            else None,
        )
    assignment = eligibility.assignment
    presence = db.scalar(
        select(FieldTechPresence).where(
            FieldTechPresence.technician_id == assignment.technician_id
        )
    )
    if presence is None or not presence.location_sharing_enabled:
        return TechnicianLocation(
            available=False,
            reason="sharing_off",
            work_order_id=query.work_order_public_id,
        )
    ping = db.scalar(
        select(FieldTechLocationPing)
        .where(
            FieldTechLocationPing.technician_id == assignment.technician_id,
            FieldTechLocationPing.crm_work_order_id == query.work_order_public_id,
            FieldTechLocationPing.assignment_generation == assignment.generation,
        )
        .order_by(
            FieldTechLocationPing.captured_at.desc(),
            FieldTechLocationPing.received_at.desc(),
        )
        .limit(1)
    )
    if ping is None:
        return TechnicianLocation(
            available=False, reason="no_fix", work_order_id=query.work_order_public_id
        )
    captured = (
        ping.captured_at
        if ping.captured_at.tzinfo
        else ping.captured_at.replace(tzinfo=UTC)
    )
    now = datetime.now(UTC)
    if not now - TECHNICIAN_LOCATION_FRESHNESS <= captured <= now:
        return TechnicianLocation(
            available=False, reason="stale", work_order_id=query.work_order_public_id
        )
    row = _owned_location_work_order(db, query)
    return TechnicianLocation(
        available=True,
        work_order_id=query.work_order_public_id,
        latitude=ping.latitude,
        longitude=ping.longitude,
        accuracy_m=ping.accuracy_m,
        updated_at=captured,
        estimated_arrival_at=row.estimated_arrival_at if row else None,
    )


class TechnicianRatingReason(StrEnum):
    not_found = "work_order_not_found"
    not_completed = "work_order_not_completed"
    not_technician = "native_technician_unavailable"
    already_rated = "already_rated"


@dataclass(frozen=True, slots=True)
class TechnicianRatingQuery:
    subscriber_id: UUID | None
    work_order_public_id: str


@dataclass(frozen=True, slots=True)
class NativeTechnicianAttribution:
    technician_id: UUID
    queue_id: UUID
    assignment_generation: UUID | None
    display_name: str | None
    phone: str | None


@dataclass(frozen=True, slots=True)
class TechnicianRatingEligibility:
    available: bool
    reason: TechnicianRatingReason | None
    technician: NativeTechnicianAttribution | None = None


class RatingRepresentativeKind(StrEnum):
    reseller_user = "reseller_user"
    subscriber = "subscriber"


@dataclass(frozen=True, slots=True)
class ResellerRatingScope:
    reseller_id: UUID
    principal_id: UUID
    principal_kind: RatingRepresentativeKind


@dataclass(frozen=True, slots=True)
class RateTechnicianCommand:
    context: CommandContext
    subscriber_id: UUID | None
    work_order_public_id: str
    rating: int
    comment: str | None = None
    representative: ResellerRatingScope | None = None


@dataclass(frozen=True, slots=True)
class TechnicianRatingOutcome:
    work_order_id: str
    rating: int
    already_rated: bool = False


@dataclass(frozen=True, slots=True)
class RecordedRatingAttribution:
    technician_id: UUID
    queue_id: UUID
    assignment_generation: UUID | None


class TechnicianRatingError(DomainError):
    pass


def _rating_error(reason: TechnicianRatingReason | str) -> TechnicianRatingError:
    return TechnicianRatingError(
        code=f"customer.work_order_selfcare.{reason}",
        message=str(reason),
        retryable=False,
    )


def recorded_rating_attribution(row: WorkOrder) -> RecordedRatingAttribution | None:
    """Rating-time attribution only; never infer the author from today's queue."""
    feedback = (row.metadata_ or {}).get("technician_rating")
    if not isinstance(feedback, dict):
        return None
    try:
        return RecordedRatingAttribution(
            technician_id=UUID(feedback["technician_id"]),
            queue_id=UUID(feedback["assignment_queue_id"]),
            assignment_generation=UUID(feedback["assignment_generation"])
            if feedback.get("assignment_generation")
            else None,
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _native_technician(
    db: Session, row: WorkOrder
) -> NativeTechnicianAttribution | None:
    assignment = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(row.id)
    )
    if (
        assignment is None
        or assignment.vendor_id is not None
        or assignment.technician_id is None
    ):
        return None
    profile = db.get(
        TechnicianProfile, assignment.technician_id, populate_existing=True
    )
    if profile is None or not profile.is_active:
        return None
    person = (
        db.get(SystemUser, profile.system_user_id, populate_existing=True)
        if profile.system_user_id
        else None
    )
    if person is None or not person.is_active:
        return None
    # A retained CRM/person UUID or vendor display projection cannot name an
    # actual staff person. Reviewed portrait/badge authority stays elsewhere.
    name = person.display_name or f"{person.first_name} {person.last_name}".strip()
    return NativeTechnicianAttribution(
        profile.id,
        assignment.queue_id,
        assignment.generation,
        name,
        person.phone,
    )


def _rating_eligibility(db: Session, row: WorkOrder) -> TechnicianRatingEligibility:
    technician = _native_technician(db, row)
    if row.technician_rating is not None:
        return TechnicianRatingEligibility(
            False, TechnicianRatingReason.already_rated, technician
        )
    if row.status not in _RATABLE_STATUSES:
        return TechnicianRatingEligibility(
            False, TechnicianRatingReason.not_completed, technician
        )
    if technician is None:
        return TechnicianRatingEligibility(False, TechnicianRatingReason.not_technician)
    return TechnicianRatingEligibility(True, None, technician)


def rating_eligibility(
    db: Session, *, query: TechnicianRatingQuery
) -> TechnicianRatingEligibility:
    if query.subscriber_id is None:
        return TechnicianRatingEligibility(False, TechnicianRatingReason.not_found)
    row = db.scalar(
        select(WorkOrder)
        .where(
            WorkOrder.subscriber_id == query.subscriber_id,
            WorkOrder.public_id == query.work_order_public_id,
            WorkOrder.is_active.is_(True),
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return TechnicianRatingEligibility(False, TechnicianRatingReason.not_found)
    return _rating_eligibility(db, row)


def rate_technician(
    db: Session, *, command: RateTechnicianCommand
) -> TechnicianRatingOutcome:
    """One rating per owned visit; the root lock serializes all submissions."""

    def operation() -> TechnicianRatingOutcome:
        # Account ownership must remain stable through both writes and replay.
        # All rating commands use Subscriber -> Reseller -> ResellerUser -> WO.
        subscriber = db.scalar(
            select(Subscriber)
            .where(Subscriber.id == command.subscriber_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subscriber is None or command.subscriber_id is None:
            raise _rating_error(TechnicianRatingReason.not_found)
        representative = command.representative
        if representative is not None:
            reseller = db.scalar(
                select(Reseller)
                .where(
                    Reseller.id == representative.reseller_id,
                    Reseller.is_active.is_(True),
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            binding_query = select(ResellerUser).where(ResellerUser.is_active.is_(True))
            if representative.principal_kind is RatingRepresentativeKind.reseller_user:
                binding_query = binding_query.where(
                    ResellerUser.id == representative.principal_id
                )
            elif representative.principal_kind is RatingRepresentativeKind.subscriber:
                binding_query = (
                    binding_query.where(
                        ResellerUser.subscriber_id == representative.principal_id
                    )
                    .order_by(ResellerUser.created_at.desc())
                    .limit(1)
                )
            else:
                raise _rating_error(TechnicianRatingReason.not_found)
            binding = db.scalar(
                binding_query.with_for_update(read=True).execution_options(
                    populate_existing=True
                )
            )
            if (
                reseller is None
                or binding is None
                or binding.reseller_id != representative.reseller_id
                or subscriber.reseller_id != representative.reseller_id
            ):
                raise _rating_error(TechnicianRatingReason.not_found)
        row: WorkOrder | None = db.scalar(
            select(WorkOrder)
            .where(
                WorkOrder.subscriber_id == command.subscriber_id,
                WorkOrder.public_id == command.work_order_public_id,
                WorkOrder.is_active.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or command.subscriber_id is None:
            raise _rating_error(TechnicianRatingReason.not_found)
        # Historical feedback is readable after ownership, even if the original
        # technician is no longer active. It never creates a new attribution.
        if row.technician_rating is not None:
            return TechnicianRatingOutcome(row.public_id, row.technician_rating, True)
        eligibility = _rating_eligibility(db, row)
        if not eligibility.available or eligibility.technician is None:
            raise _rating_error(
                eligibility.reason or TechnicianRatingReason.not_technician
            )
        if (
            isinstance(command.rating, bool)
            or not isinstance(command.rating, int)
            or not 1 <= command.rating <= 5
        ):
            raise _rating_error("invalid_rating")
        technician = eligibility.technician
        rated_at = datetime.now(UTC)
        metadata = dict(row.metadata_ or {})
        metadata["technician_rating"] = {
            "rating": command.rating,
            "comment": (command.comment or "").strip()[:2000] or None,
            "rated_at": rated_at.isoformat(),
            "source": "customer_selfcare",
            "technician_id": str(technician.technician_id),
            "assignment_queue_id": str(technician.queue_id),
            "assignment_generation": str(technician.assignment_generation)
            if technician.assignment_generation
            else None,
        }
        row.metadata_ = metadata
        details = {
            "owner": "customer.work_order_selfcare",
            "subscriber_id": str(row.subscriber_id),
            "work_order_public_id": row.public_id,
            "rating": command.rating,
            "technician_id": str(technician.technician_id),
            "assignment_queue_id": str(technician.queue_id),
            "assignment_generation": str(technician.assignment_generation)
            if technician.assignment_generation
            else None,
        }
        stage_audit_event(
            db,
            action="work_order_technician_rated",
            entity_type="work_order",
            entity_id=str(row.id),
            actor=AuditActor.system(command.context.actor),
            request_id=str(command.context.correlation_id),
            metadata=details,
        )
        emit_event(
            db,
            EventType.custom,
            {
                "name": "work_order.technician_rated",
                "schema_version": 1,
                "work_order_id": str(row.id),
                **details,
            },
            actor=command.context.actor,
            subscriber_id=row.subscriber_id,
            dispatch_after_commit=False,
        )
        db.flush()
        return TechnicianRatingOutcome(row.public_id, command.rating)

    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner="customer.work_order_selfcare",
            concern="canonical customer technician rating",
            name="rate_technician",
        ),
        context=command.context,
        operation=operation,
    )
