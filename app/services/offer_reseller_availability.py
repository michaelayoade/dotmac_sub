"""Canonical owner for explicit reseller-specific catalog offer access."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import CatalogOffer, OfferStatus
from app.models.offer_availability import OfferResellerAvailability
from app.models.subscriber import Reseller
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

RESELLER_OFFER_AVAILABILITY_SCOPE = "reseller:write"
MAX_ASSIGNMENTS_PER_RESELLER = 500

_SET_ASSIGNMENTS_COMMAND = OwnerCommandDefinition(
    owner="service_intent.offer_reseller_availability",
    concern="reseller-specific catalog offer availability",
    name="set_reseller_offer_availability",
)


class OfferResellerAvailabilityError(DomainError):
    """Stable, transport-neutral reseller catalog-access failure."""


@dataclass(frozen=True, slots=True)
class SetResellerOfferAvailabilityCommand:
    context: CommandContext
    reseller_id: UUID
    offer_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ResellerOfferAvailabilityOutcome:
    reseller_id: UUID
    active_offer_ids: tuple[UUID, ...]
    added_offer_ids: tuple[UUID, ...]
    reactivated_offer_ids: tuple[UUID, ...]
    deactivated_offer_ids: tuple[UUID, ...]
    changed: bool
    command_id: UUID
    correlation_id: UUID


def _error(
    code: str,
    message: str,
    **details: object,
) -> OfferResellerAvailabilityError:
    return OfferResellerAvailabilityError(
        code=f"service_intent.offer_reseller_availability.{code}",
        message=message,
        details=details,
    )


def _validated_actor(context: CommandContext) -> AuditActor:
    if context.scope != RESELLER_OFFER_AVAILABILITY_SCOPE:
        raise _error(
            "invalid_command",
            "Reseller catalog access requires reseller write authorization.",
            field="scope",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Reseller catalog access actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "Reseller catalog access actor identity is incomplete.",
            field="actor",
        )
    return AuditActor(actor_type=actor_type, actor_id=actor_id.strip())


def set_reseller_offer_availability(
    db: Session,
    command: SetResellerOfferAvailabilityCommand,
) -> ResellerOfferAvailabilityOutcome:
    """Replace one reseller's active assignments while preserving row history."""

    def operation() -> ResellerOfferAvailabilityOutcome:
        audit_actor = _validated_actor(command.context)
        desired_offer_ids = set(command.offer_ids)
        if len(desired_offer_ids) != len(command.offer_ids):
            raise _error(
                "invalid_command",
                "Duplicate reseller offer assignments are not allowed.",
            )
        if len(desired_offer_ids) > MAX_ASSIGNMENTS_PER_RESELLER:
            raise _error(
                "invalid_command",
                "Reseller offer assignment exceeds the supported cardinality.",
                maximum=MAX_ASSIGNMENTS_PER_RESELLER,
            )

        reseller = db.execute(
            select(Reseller).where(Reseller.id == command.reseller_id).with_for_update()
        ).scalar_one_or_none()
        if reseller is None:
            raise _error(
                "reseller_not_found",
                "Reseller was not found.",
                reseller_id=str(command.reseller_id),
            )

        if desired_offer_ids:
            active_offer_ids = set(
                db.scalars(
                    select(CatalogOffer.id)
                    .where(CatalogOffer.id.in_(desired_offer_ids))
                    .where(CatalogOffer.is_active.is_(True))
                    .where(CatalogOffer.status == OfferStatus.active)
                    .order_by(CatalogOffer.id)
                    .with_for_update()
                ).all()
            )
            missing_offer_ids = desired_offer_ids - active_offer_ids
            if missing_offer_ids:
                raise _error(
                    "offer_not_available",
                    "Only active catalog offers can be assigned to a reseller.",
                    offer_ids=tuple(str(value) for value in sorted(missing_offer_ids)),
                )

        existing_rows = tuple(
            db.scalars(
                select(OfferResellerAvailability)
                .where(OfferResellerAvailability.reseller_id == command.reseller_id)
                .order_by(OfferResellerAvailability.offer_id)
                .with_for_update()
            ).all()
        )
        existing_by_offer = {row.offer_id: row for row in existing_rows}

        added: list[UUID] = []
        reactivated: list[UUID] = []
        deactivated: list[UUID] = []
        for offer_id, row in existing_by_offer.items():
            should_be_active = offer_id in desired_offer_ids
            if row.is_active and not should_be_active:
                row.is_active = False
                deactivated.append(offer_id)
            elif not row.is_active and should_be_active:
                row.is_active = True
                reactivated.append(offer_id)

        for offer_id in desired_offer_ids - set(existing_by_offer):
            db.add(
                OfferResellerAvailability(
                    offer_id=offer_id,
                    reseller_id=command.reseller_id,
                    is_active=True,
                )
            )
            added.append(offer_id)

        try:
            db.flush()
        except IntegrityError as exc:
            raise _error(
                "assignment_conflict",
                "Reseller offer assignments conflict with current catalog state.",
            ) from exc

        added_ids = tuple(sorted(added))
        reactivated_ids = tuple(sorted(reactivated))
        deactivated_ids = tuple(sorted(deactivated))
        changed = bool(added_ids or reactivated_ids or deactivated_ids)
        if changed:
            change_payload: dict[str, object] = {
                "reseller_id": str(command.reseller_id),
                "added_offer_ids": [str(value) for value in added_ids],
                "reactivated_offer_ids": [str(value) for value in reactivated_ids],
                "deactivated_offer_ids": [str(value) for value in deactivated_ids],
            }
            stage_audit_event(
                db,
                action="catalog_access_updated",
                entity_type="reseller",
                entity_id=str(command.reseller_id),
                actor=audit_actor,
                metadata=change_payload,
            )
            emit_event(
                db,
                EventType.catalog_offer_reseller_availability_changed,
                change_payload,
                actor=command.context.actor,
            )

        return ResellerOfferAvailabilityOutcome(
            reseller_id=command.reseller_id,
            active_offer_ids=tuple(sorted(desired_offer_ids)),
            added_offer_ids=added_ids,
            reactivated_offer_ids=reactivated_ids,
            deactivated_offer_ids=deactivated_ids,
            changed=changed,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_SET_ASSIGNMENTS_COMMAND,
        context=command.context,
        operation=operation,
    )
