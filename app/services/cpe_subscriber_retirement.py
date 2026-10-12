"""Flush-only active CPE retirement for exact customer account hard deletion."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.network import CPEDevice, DeviceStatus
from app.services.audit_adapter import AuditActor
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    current_command_context,
    current_command_definition,
    owner_command_active,
)


@dataclass(frozen=True, slots=True)
class RetireSubscriberCPECommand:
    context: CommandContext = field(repr=False)
    subscriber_id: UUID
    actor: AuditActor = field(repr=False)


@dataclass(frozen=True, slots=True)
class SubscriberCPERetirementOutcome:
    retired_cpe_ids: tuple[UUID, ...]


class SubscriberCPERetirementError(DomainError):
    """Stable refusal of an untrusted customer deletion participant."""


def stage_subscriber_cpe_retirement(
    db: Session, *, command: RetireSubscriberCPECommand
) -> SubscriberCPERetirementOutcome:
    """Retire only active rows after the coordinator locks the exact Subscriber.

    Parent lock and refresh are caller preconditions. Sorted exact child locks
    use NOWAIT: contention must roll back the whole owner command before retry.
    Rows and nonactive history survive; migrated FKs detach them on deletion.
    """
    if (
        not isinstance(command, RetireSubscriberCPECommand)
        or not isinstance(command.subscriber_id, UUID)
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or command.actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or command.context.actor
        != f"{command.actor.actor_type.value}:{command.actor.actor_id}"
        or not owner_command_active(db, owner="customer.account_deletion")
        or current_command_definition(db)
        != OwnerCommandDefinition(
            owner="customer.account_deletion",
            concern="customer account hard deletion",
            name="delete_customer_account",
        )
        or current_command_context(db) != command.context
    ):
        raise SubscriberCPERetirementError(
            code="network.cpe_subscriber_retirement.invalid_customer_deletion_participant",
            message="Exact trusted customer deletion command required.",
        )
    with db.no_autoflush:
        devices = tuple(
            db.scalars(
                select(CPEDevice)
                .where(CPEDevice.subscriber_id == command.subscriber_id)
                .order_by(CPEDevice.id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        )
    retired: list[UUID] = []
    for device in devices:
        if device.status is DeviceStatus.active:
            device.status = DeviceStatus.retired
            retired.append(device.id)
    db.flush()
    return SubscriberCPERetirementOutcome(tuple(retired))
