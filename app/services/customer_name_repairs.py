"""Evidence-bound repair owner for legacy Subscriber names."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from string import hexdigits
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.customer_identity_normalization import is_placeholder_customer_name
from app.services.customer_identity_resolution import (
    rebuild_identity_index_for_subscriber,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

ResultT = TypeVar("ResultT")
IDENTITY_FIELDS = ("first_name", "last_name", "display_name")
SOURCE_ACTION = "crm_customer_identity_update"
ITEM_AUDIT_ACTION = "crm_placeholder_name_remediated"
BATCH_AUDIT_ACTION = "crm_placeholder_name_remediation_applied"


class CustomerNameRepairError(DomainError):
    """Stable failure from the legacy name-repair boundary."""


def _error(suffix: str, message: str, **details: object) -> CustomerNameRepairError:
    return CustomerNameRepairError(
        code=f"customer.name_repairs.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class CustomerNameState:
    first_name: str | None
    last_name: str | None
    display_name: str | None

    @classmethod
    def from_subscriber(cls, subscriber: Subscriber) -> CustomerNameState:
        return cls(
            first_name=subscriber.first_name,
            last_name=subscriber.last_name,
            display_name=subscriber.display_name,
        )


@dataclass(frozen=True, slots=True)
class CustomerNameRepairItem:
    subscriber_id: UUID
    expected_current: CustomerNameState
    replacement: CustomerNameState
    source_audit_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class RepairCustomerNamesCommand:
    context: CommandContext
    manifest_digest: str
    target: str
    repairs: tuple[CustomerNameRepairItem, ...]


@dataclass(frozen=True, slots=True)
class RepairCustomerNamesOutcome:
    manifest_digest: str
    applied_count: int
    already_applied: bool


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(
        owner="customer.name_repairs",
        concern="evidence-bound legacy Subscriber name repair",
        name=name,
    )


def _execute(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    operation: Callable[[], ResultT],
) -> ResultT:
    return execute_owner_command(
        db,
        definition=_definition(name),
        context=context,
        operation=operation,
    )


def _validate_digest(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(char not in hexdigits for char in normalized):
        raise _error("invalid_manifest", "Repair manifest digest is invalid.")
    return normalized


def _validate_replacement(item: CustomerNameRepairItem) -> None:
    first_name = str(item.replacement.first_name or "").strip()
    last_name = str(item.replacement.last_name or "").strip()
    display_name = str(item.replacement.display_name or "").strip()
    candidate = display_name or f"{first_name} {last_name}".strip()
    if not first_name or not last_name or is_placeholder_customer_name(candidate):
        raise _error(
            "invalid_replacement",
            "Repair replacement is incomplete or generic.",
            subscriber_id=str(item.subscriber_id),
        )
    if not item.source_audit_ids:
        raise _error(
            "missing_evidence",
            "Repair has no source audit evidence.",
            subscriber_id=str(item.subscriber_id),
        )


def _audit_changes(event: AuditEvent) -> dict[str, dict[str, object]]:
    metadata = event.metadata_ if isinstance(event.metadata_, dict) else {}
    changes = metadata.get("changes")
    return changes if isinstance(changes, dict) else {}


def _validate_source_evidence(
    db: Session,
    item: CustomerNameRepairItem,
) -> None:
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.id.in_(item.source_audit_ids))
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        ).all()
    )
    if len(events) != len(set(item.source_audit_ids)):
        raise _error(
            "missing_evidence",
            "Repair source audit evidence is missing.",
            subscriber_id=str(item.subscriber_id),
        )
    first_old: dict[str, str | None] = {}
    latest_new: dict[str, str | None] = {}
    for event in events:
        if event.action != SOURCE_ACTION or event.entity_id != str(item.subscriber_id):
            raise _error(
                "invalid_evidence",
                "Repair evidence does not belong to the requested incident account.",
                subscriber_id=str(item.subscriber_id),
            )
        changes = _audit_changes(event)
        for field in IDENTITY_FIELDS:
            change = changes.get(field)
            if (
                not isinstance(change, dict)
                or "old" not in change
                or "new" not in change
            ):
                raise _error(
                    "invalid_evidence",
                    "Repair evidence is missing an exact identity transition.",
                    subscriber_id=str(item.subscriber_id),
                    field=field,
                )
            old = change.get("old")
            new = change.get("new")
            old_value = None if old is None else str(old)
            new_value = None if new is None else str(new)
            if field in latest_new and old_value != latest_new[field]:
                raise _error(
                    "invalid_evidence",
                    "Repair evidence contains a broken identity transition chain.",
                    subscriber_id=str(item.subscriber_id),
                    field=field,
                )
            first_old.setdefault(field, old_value)
            latest_new[field] = new_value

    replacement = item.replacement
    expected = item.expected_current
    for field in IDENTITY_FIELDS:
        original = first_old.get(field)
        incident = latest_new.get(field)
        replacement_value = getattr(replacement, field)
        expected_value = getattr(expected, field)
        if replacement_value != original or expected_value not in {
            original,
            incident,
        }:
            raise _error(
                "invalid_evidence",
                "Repair manifest does not match its audit evidence.",
                subscriber_id=str(item.subscriber_id),
                field=field,
            )


def _repair_customer_names(
    db: Session,
    command: RepairCustomerNamesCommand,
) -> RepairCustomerNamesOutcome:
    digest = _validate_digest(command.manifest_digest)
    target = command.target.strip()
    if not target:
        raise _error("invalid_manifest", "Repair target is required.")
    if not command.repairs:
        raise _error("invalid_manifest", "Repair manifest has no repair items.")
    repair_ids = tuple(
        sorted({item.subscriber_id for item in command.repairs}, key=str)
    )
    if len(repair_ids) != len(command.repairs):
        raise _error("invalid_manifest", "Repair manifest repeats a subscriber.")

    prior = db.scalars(
        select(AuditEvent.id).where(
            AuditEvent.action == BATCH_AUDIT_ACTION,
            AuditEvent.entity_type == "crm_placeholder_name_remediation",
            AuditEvent.entity_id == digest,
        )
    ).first()
    if prior is not None:
        return RepairCustomerNamesOutcome(
            manifest_digest=digest,
            applied_count=0,
            already_applied=True,
        )

    subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(Subscriber.id.in_(repair_ids))
            .order_by(Subscriber.id)
            .with_for_update()
        ).all()
    )
    by_id = {subscriber.id: subscriber for subscriber in subscribers}
    if set(by_id) != set(repair_ids):
        raise _error(
            "missing_subscriber", "Repair manifest contains a missing account."
        )

    for item in command.repairs:
        _validate_replacement(item)
        _validate_source_evidence(db, item)
        subscriber = by_id[item.subscriber_id]
        if subscriber.party_id is not None:
            raise _error(
                "party_bound",
                "Party-bound identity must be repaired by party.registry.",
                subscriber_id=str(subscriber.id),
            )
        if CustomerNameState.from_subscriber(subscriber) != item.expected_current:
            raise _error(
                "stale_manifest",
                "Customer name changed after the repair was planned.",
                subscriber_id=str(subscriber.id),
            )

        before = CustomerNameState.from_subscriber(subscriber)
        subscriber.first_name = item.replacement.first_name or ""
        subscriber.last_name = item.replacement.last_name or ""
        subscriber.display_name = item.replacement.display_name
        rebuild_identity_index_for_subscriber(db, subscriber.id)
        changes = {
            field: {
                "old": getattr(before, field),
                "new": getattr(item.replacement, field),
            }
            for field in IDENTITY_FIELDS
            if getattr(before, field) != getattr(item.replacement, field)
        }
        audit_service.audit_events.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.service,
                actor_id=command.context.actor,
                action=ITEM_AUDIT_ACTION,
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                status_code=200,
                is_success=True,
                metadata_={
                    "reason": command.context.reason,
                    "target": target,
                    "manifest_digest": digest,
                    "source_audit_ids": [str(value) for value in item.source_audit_ids],
                    "changes": changes,
                },
            ),
        )
        emit_event(
            db,
            EventType.subscriber_updated,
            {
                "subscriber_id": str(subscriber.id),
                "changed_fields": list(changes),
                "reason": "crm_placeholder_name_remediation",
                "manifest_digest": digest,
            },
            actor=command.context.actor,
            subscriber_id=subscriber.id,
        )

    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.service,
            actor_id=command.context.actor,
            action=BATCH_AUDIT_ACTION,
            entity_type="crm_placeholder_name_remediation",
            entity_id=digest,
            status_code=200,
            is_success=True,
            metadata_={
                "reason": command.context.reason,
                "target": target,
                "manifest_digest": digest,
                "applied_count": len(command.repairs),
                "subscriber_ids": [str(value) for value in repair_ids],
            },
        ),
    )
    db.flush()
    return RepairCustomerNamesOutcome(
        manifest_digest=digest,
        applied_count=len(command.repairs),
        already_applied=False,
    )


def repair_customer_names(
    db: Session,
    command: RepairCustomerNamesCommand,
) -> RepairCustomerNamesOutcome:
    """Apply one exact repair manifest in the registered owner transaction."""

    return _execute(
        db,
        context=command.context,
        name="repair_customer_names",
        operation=lambda: _repair_customer_names(db, command),
    )
