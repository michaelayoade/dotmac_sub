"""Explicit flush-only participant for subscriber registration metadata."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.credential_administration import error
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext, owner_command_active


@dataclass(frozen=True, slots=True)
class ApplyRegistrationMetadata:
    context: CommandContext
    subscriber_id: UUID
    business: bool
    subscriber_number: str | None
    category: SubscriberCategory | None
    notes: str | None
    number_prefix: str


def apply_registration_metadata(
    db: Session, *, command: ApplyRegistrationMetadata
) -> None:
    if not owner_command_active(db, owner="auth.credential_administration"):
        raise error(
            "invalid_command_context",
            "Registration participant requires its administrative coordinator.",
        )
    row = db.get(Subscriber, command.subscriber_id)
    if row is None:
        raise error("principal_not_found", "Subscriber was not found.")
    if (row.category == SubscriberCategory.business) != command.business:
        raise error(
            "invalid_principal", "Subscriber person/business identity does not match."
        )
    number = (command.subscriber_number or "").strip() or None
    row.subscriber_number = (
        number if not number or number.startswith(command.number_prefix) else None
    )
    if command.business:
        row.category = SubscriberCategory.business
    elif command.category is not None:
        if command.category == SubscriberCategory.business:
            raise error(
                "invalid_principal", "Person registration cannot become a business."
            )
        row.category = command.category
    row.notes = (command.notes or "").strip() or None
    db.flush()
    stage_audit_event(
        db,
        action="customer.registration_metadata.updated",
        entity_type="subscriber",
        entity_id=str(row.id),
        actor=AuditActor.service(command.context.actor[8:])
        if command.context.actor.startswith("service:")
        else AuditActor.user(command.context.actor),
        metadata={
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "changed_fields": ["subscriber_number", "category", "notes"],
        },
    )
    emit_event(
        db,
        EventType.subscriber_updated,
        {
            "schema_version": 1,
            "subscriber_id": str(row.id),
            "changed_fields": ["subscriber_number", "category", "notes"],
            "source": "credential_administration",
        },
        actor=command.context.actor,
        subscriber_id=row.id,
    )
