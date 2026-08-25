"""Sub-owned field-workforce presence status.

Positioning can report where an opaque tracked unit was observed. Sub alone
decides whether one of its technicians is off shift, on shift, on break, or
busy. The legacy mixed table is retained only until the positioning module
cutover splits these product and shared projections physically.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_location import FIELD_PRESENCE_STATUSES, FieldTechPresence
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.field.jobs import _profile_from_principal
from app.services.field.location_tracking import LocationPrincipal
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

FIELD_OPERATIONS_COLLECTION_PURPOSE = "field_operations"

_UPDATE_STATUS = OwnerCommandDefinition(
    owner="operations.field_presence",
    concern="field workforce presence status",
    name="update_field_workforce_presence_status",
)


@dataclass(frozen=True)
class UpdateFieldPresenceStatusCommand:
    context: CommandContext
    principal: LocationPrincipal
    status: str


@dataclass(frozen=True)
class FieldPresenceStatusSnapshot:
    person_id: UUID
    status: str


def _error(code: str, message: str) -> DomainError:
    return DomainError(code=code, message=message)


def _profile(db: Session, principal: LocationPrincipal) -> TechnicianProfile:
    try:
        return _profile_from_principal(db, principal.as_legacy_principal())
    except HTTPException as exc:
        raise _error(
            "field_presence_technician_not_found",
            "Active technician profile was not found.",
        ) from exc


def _normalize_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized not in FIELD_PRESENCE_STATUSES:
        raise _error(
            "field_presence_invalid_status",
            f"Unsupported field presence status: {status}",
        )
    return normalized


def _get_or_create_presence(
    db: Session,
    profile: TechnicianProfile,
) -> FieldTechPresence:
    presence = (
        db.query(FieldTechPresence)
        .filter(FieldTechPresence.technician_id == profile.id)
        .one_or_none()
    )
    if presence is None:
        presence = FieldTechPresence(
            technician_id=profile.id,
            person_id=profile.person_id,
        )
        db.add(presence)
        db.flush()
    return presence


class FieldPresence:
    @staticmethod
    def get_status(
        db: Session,
        principal: LocationPrincipal,
    ) -> FieldPresenceStatusSnapshot:
        profile = _profile(db, principal)
        presence = (
            db.query(FieldTechPresence)
            .filter(FieldTechPresence.technician_id == profile.id)
            .one_or_none()
        )
        return FieldPresenceStatusSnapshot(
            person_id=profile.person_id,
            status=presence.status if presence is not None else "off_shift",
        )

    @staticmethod
    def update_status(
        db: Session,
        command: UpdateFieldPresenceStatusCommand,
    ) -> FieldPresenceStatusSnapshot:
        status = _normalize_status(command.status)

        def operation() -> FieldPresenceStatusSnapshot:
            profile = _profile(db, command.principal)
            presence = _get_or_create_presence(db, profile)
            if presence.status == status:
                return FieldPresenceStatusSnapshot(
                    person_id=profile.person_id,
                    status=status,
                )
            presence.status = status
            db.flush()
            emit_event(
                db,
                EventType.field_presence_changed,
                {
                    "technician_id": str(profile.id),
                    "status": status,
                },
                actor=command.context.actor,
            )
            return FieldPresenceStatusSnapshot(
                person_id=profile.person_id,
                status=status,
            )

        return execute_owner_command(
            db,
            definition=_UPDATE_STATUS,
            context=command.context,
            operation=operation,
        )


field_presence = FieldPresence()
