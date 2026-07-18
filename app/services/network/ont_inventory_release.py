"""Canonical electronic-identity release for return-to-inventory transitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit


@dataclass(frozen=True)
class OntInventoryIdentityReleaseResult:
    ont_unit_id: uuid.UUID
    assignment_ids: tuple[uuid.UUID, ...]
    deactivated_assignment_ids: tuple[uuid.UUID, ...]
    released_at: datetime


def release_ont_electronic_identity(
    db: Session,
    *,
    ont_unit_id: str | uuid.UUID,
    released_at: datetime | None = None,
) -> OntInventoryIdentityReleaseResult:
    """Release all customer and electronic identity from reusable ONT hardware.

    The caller owns the broader return-to-inventory orchestration and must
    complete external OLT/ACS cleanup first. This owner locks the ONT and every
    assignment, closes active assignments, and clears exact subscription,
    subscriber, service-address, PON, OLT, and F/S/P identity in one local
    transaction. It never selects a replacement identity.
    """

    try:
        normalized_ont_id = (
            ont_unit_id
            if isinstance(ont_unit_id, uuid.UUID)
            else uuid.UUID(str(ont_unit_id))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("ont_unit_id must be a UUID") from exc

    ont = db.scalar(
        select(OntUnit).where(OntUnit.id == normalized_ont_id).with_for_update()
    )
    if ont is None:
        raise ValueError("ONT not found")

    assignments = list(
        db.scalars(
            select(OntAssignment)
            .where(OntAssignment.ont_unit_id == ont.id)
            .order_by(OntAssignment.id)
            .with_for_update()
        )
    )
    timestamp = released_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)

    deactivated: list[uuid.UUID] = []
    for assignment in assignments:
        if assignment.active:
            assignment.active = False
            assignment.released_at = timestamp
            assignment.release_reason = "returned_to_inventory"
            deactivated.append(assignment.id)
        assignment.subscription_id = None
        assignment.subscriber_id = None
        assignment.service_address_id = None
        assignment.pon_port_id = None

    ont.olt_device_id = None
    ont.pon_port_id = None
    ont.board = None
    ont.port = None
    ont.external_id = None
    db.flush()

    return OntInventoryIdentityReleaseResult(
        ont_unit_id=ont.id,
        assignment_ids=tuple(row.id for row in assignments),
        deactivated_assignment_ids=tuple(deactivated),
        released_at=timestamp,
    )
