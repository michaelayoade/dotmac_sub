"""Native customer actions for Sub-owned field-service work orders.

This service owns customer-visible technician location reads and the single
customer feedback write. It never calls an external work system and scopes
every operation to the subscriber before returning data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.domain_settings import SettingDomain
from app.models.field_location import FieldTechPresence
from app.models.work_order import WorkOrder
from app.schemas.portal import TechnicianLocation, TechnicianRatingResponse
from app.services.audit_helpers import log_audit_event
from app.services.common import coerce_uuid
from app.services.settings_spec import resolve_integer

_TRACKABLE_STATUSES = frozenset({"in_progress"})
_RATABLE_STATUSES = frozenset({"completed"})


def _owned_work_order(
    db: Session, subscriber_id: str, work_order_public_id: str
) -> WorkOrder | None:
    try:
        subscriber_uuid = coerce_uuid(subscriber_id)
    except (TypeError, ValueError):
        return None
    return db.scalar(
        select(WorkOrder).where(
            WorkOrder.subscriber_id == subscriber_uuid,
            WorkOrder.public_id == work_order_public_id,
            WorkOrder.is_active.is_(True),
        )
    )


def _assigned_profile(db: Session, row: WorkOrder) -> TechnicianProfile | None:
    assignment = db.scalar(
        select(WorkOrderAssignmentQueue)
        .where(
            WorkOrderAssignmentQueue.work_order_mirror_id == row.id,
            WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
            WorkOrderAssignmentQueue.assigned_technician_id.is_not(None),
        )
        .order_by(WorkOrderAssignmentQueue.updated_at.desc())
    )
    if assignment is None or assignment.assigned_technician_id is None:
        return None
    return db.get(TechnicianProfile, assignment.assigned_technician_id)


def technician_location(
    db: Session, subscriber_id: str, work_order_public_id: str
) -> TechnicianLocation:
    """Read the canonical Sub field presence for an active customer visit."""
    row = _owned_work_order(db, subscriber_id, work_order_public_id)
    if row is None:
        return TechnicianLocation(available=False, reason="not_found")
    if row.status not in _TRACKABLE_STATUSES or row.completed_at is not None:
        return TechnicianLocation(
            available=False,
            reason="not_active",
            work_order_id=row.public_id,
        )

    profile = _assigned_profile(db, row)
    if profile is None:
        return TechnicianLocation(
            available=False,
            reason="not_assigned",
            work_order_id=row.public_id,
        )
    presence = db.scalar(
        select(FieldTechPresence).where(FieldTechPresence.technician_id == profile.id)
    )
    if presence is None or not presence.location_sharing_enabled:
        return TechnicianLocation(
            available=False,
            reason="sharing_off",
            work_order_id=row.public_id,
        )
    now = datetime.now(UTC)
    collection_expires_at = presence.collection_expires_at
    if collection_expires_at is not None and collection_expires_at.tzinfo is None:
        collection_expires_at = collection_expires_at.replace(tzinfo=UTC)
    if (
        presence.collection_purpose != "field_operations"
        or collection_expires_at is None
        or collection_expires_at <= now
    ):
        return TechnicianLocation(
            available=False,
            reason="collection_grant_expired",
            work_order_id=row.public_id,
        )
    if presence.last_latitude is None or presence.last_longitude is None:
        return TechnicianLocation(
            available=False,
            reason="no_fix",
            work_order_id=row.public_id,
        )
    last_location_at = presence.last_location_at
    if last_location_at is None:
        return TechnicianLocation(
            available=False,
            reason="no_fix",
            work_order_id=row.public_id,
        )
    if last_location_at.tzinfo is None:
        last_location_at = last_location_at.replace(tzinfo=UTC)
    stale_after_seconds = resolve_integer(
        db,
        SettingDomain.field,
        "location_customer_stale_seconds",
    )
    if last_location_at < now - timedelta(seconds=stale_after_seconds):
        return TechnicianLocation(
            available=False,
            reason="stale_fix",
            work_order_id=row.public_id,
        )
    return TechnicianLocation(
        available=True,
        work_order_id=row.public_id,
        latitude=presence.last_latitude,
        longitude=presence.last_longitude,
        accuracy_m=presence.last_location_accuracy_m,
        updated_at=presence.last_location_at,
        estimated_arrival_at=row.estimated_arrival_at,
    )


def rate_technician(
    db: Session,
    subscriber_id: str,
    work_order_public_id: str,
    *,
    rating: int,
    comment: str | None = None,
) -> TechnicianRatingResponse:
    """Persist the one canonical customer rating for a completed field visit."""
    row = _owned_work_order(db, subscriber_id, work_order_public_id)
    if row is None:
        raise LookupError("work_order_not_found")
    if row.technician_rating is not None:
        return TechnicianRatingResponse(
            already_rated=True,
            rating=row.technician_rating,
            work_order_id=row.public_id,
        )
    if row.status not in _RATABLE_STATUSES:
        raise ValueError("work_order_not_completed")

    normalized_rating = max(1, min(5, int(rating)))
    metadata = dict(row.metadata_ or {})
    metadata["technician_rating"] = {
        "rating": normalized_rating,
        "comment": (comment or "").strip()[:2000] or None,
        "rated_at": datetime.now(UTC).isoformat(),
        "source": "customer_selfcare",
    }
    row.metadata_ = metadata
    log_audit_event(
        db=db,
        request=None,
        action="work_order_technician_rated",
        entity_type="work_order",
        entity_id=str(row.id),
        actor_id=None,
        metadata={
            "owner": "customer.work_order_selfcare",
            "subscriber_id": str(row.subscriber_id),
            "work_order_public_id": row.public_id,
            "rating": normalized_rating,
        },
    )
    db.commit()
    return TechnicianRatingResponse(
        rating=normalized_rating,
        work_order_id=row.public_id,
    )
