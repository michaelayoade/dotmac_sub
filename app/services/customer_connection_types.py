"""Owner for reusable customer connection type definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.customer_connection_type import CustomerConnectionTypeDefinition
from app.models.subscriber import Subscriber
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

READ_SCOPE = "system:settings:read"
WRITE_SCOPE = "system:settings:write"

_SAVE_DEFINITION = OwnerCommandDefinition(
    owner="customer.connection_types",
    concern="customer connection type definition lifecycle",
    name="save_customer_connection_type",
)
_DISABLE_DEFINITION = OwnerCommandDefinition(
    owner="customer.connection_types",
    concern="customer connection type definition lifecycle",
    name="disable_customer_connection_type",
)


class CustomerConnectionTypeError(DomainError):
    """Stable transport-neutral connection-type failure."""


def _error(suffix: str, message: str, **details: object) -> CustomerConnectionTypeError:
    return CustomerConnectionTypeError(
        code=f"customer.connection_types.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class ConnectionTypeDefinition:
    id: UUID
    code: str
    name: str
    description: str | None
    sort_order: int
    is_active: bool
    assigned_customer_count: int


@dataclass(frozen=True, slots=True)
class ConnectionTypeOption:
    code: str
    name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class SaveConnectionTypeCommand:
    context: CommandContext
    definition_id: UUID | None
    name: str
    description: str | None
    sort_order: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class DisableConnectionTypeCommand:
    context: CommandContext
    definition_id: UUID


@dataclass(frozen=True, slots=True)
class ConnectionTypeOutcome:
    definition_id: UUID
    code: str
    name: str
    is_active: bool
    created: bool


def _definition_projection(
    definition: CustomerConnectionTypeDefinition, assigned_customer_count: int
) -> ConnectionTypeDefinition:
    return ConnectionTypeDefinition(
        id=definition.id,
        code=definition.code,
        name=definition.name,
        description=definition.description,
        sort_order=definition.sort_order,
        is_active=definition.is_active,
        assigned_customer_count=assigned_customer_count,
    )


def list_definitions(db: Session) -> list[ConnectionTypeDefinition]:
    """Return all configured definitions and their current usage counts."""

    assigned_count = func.count(Subscriber.id)
    rows = db.execute(
        select(CustomerConnectionTypeDefinition, assigned_count)
        .outerjoin(
            Subscriber,
            Subscriber.connection_type == CustomerConnectionTypeDefinition.code,
        )
        .group_by(CustomerConnectionTypeDefinition.id)
        .order_by(
            CustomerConnectionTypeDefinition.sort_order.asc(),
            CustomerConnectionTypeDefinition.name.asc(),
        )
    ).all()
    return [
        _definition_projection(definition, int(count or 0))
        for definition, count in rows
    ]


def get_definition(
    db: Session, *, definition_id: UUID
) -> ConnectionTypeDefinition | None:
    for definition in list_definitions(db):
        if definition.id == definition_id:
            return definition
    return None


def connection_type_options(
    db: Session, *, current_code: str | None = None
) -> list[ConnectionTypeOption]:
    """Return assignable types plus an inactive current assignment, if any."""

    query = select(CustomerConnectionTypeDefinition).where(
        CustomerConnectionTypeDefinition.is_active.is_(True)
    )
    if current_code:
        query = select(CustomerConnectionTypeDefinition).where(
            (CustomerConnectionTypeDefinition.is_active.is_(True))
            | (CustomerConnectionTypeDefinition.code == current_code)
        )
    definitions = db.scalars(
        query.order_by(
            CustomerConnectionTypeDefinition.sort_order.asc(),
            CustomerConnectionTypeDefinition.name.asc(),
        )
    ).all()
    return [
        ConnectionTypeOption(
            code=definition.code,
            name=definition.name,
            is_active=definition.is_active,
        )
        for definition in definitions
    ]


def validate_assignment(
    db: Session,
    *,
    connection_type: str | None,
    current_code: str | None = None,
) -> str | None:
    """Resolve a submitted code and refuse unknown or newly selected inactive types."""

    code = (connection_type or "").strip().lower()
    if not code:
        return None
    definition = db.scalar(
        select(CustomerConnectionTypeDefinition).where(
            CustomerConnectionTypeDefinition.code == code
        )
    )
    if definition is None:
        raise _error(
            "unknown_type",
            "Select a configured customer connection type.",
            connection_type=code,
        )
    if not definition.is_active and code != current_code:
        raise _error(
            "inactive_type",
            "The selected customer connection type is inactive.",
            connection_type=code,
        )
    return definition.code


def _normalize_name(name: str) -> str:
    normalized = " ".join(name.split())
    if not normalized:
        raise _error("invalid_type", "Connection type name is required", field="name")
    if len(normalized) > 80:
        raise _error(
            "invalid_type",
            "Connection type name must be 80 characters or fewer.",
            field="name",
        )
    return normalized


def _code_from_name(name: str) -> str:
    code = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not code:
        raise _error(
            "invalid_type",
            "Connection type name must contain at least one letter or number.",
            field="name",
        )
    return code[:64].rstrip("-")


def save_definition(
    db: Session, command: SaveConnectionTypeCommand
) -> ConnectionTypeOutcome:
    """Create or update one definition in the owner's atomic transaction."""

    def operation() -> ConnectionTypeOutcome:
        name = _normalize_name(command.name)
        description = (
            command.description.strip()
            if command.description and command.description.strip()
            else None
        )
        if description and len(description) > 500:
            raise _error(
                "invalid_type",
                "Description must be 500 characters or fewer.",
                field="description",
            )
        created = command.definition_id is None
        definition: CustomerConnectionTypeDefinition | None
        if created:
            definition = CustomerConnectionTypeDefinition(code=_code_from_name(name))
        else:
            definition = db.scalar(
                select(CustomerConnectionTypeDefinition)
                .where(CustomerConnectionTypeDefinition.id == command.definition_id)
                .with_for_update()
            )
        if definition is None:
            raise _error(
                "type_not_found",
                "Customer connection type not found.",
                definition_id=str(command.definition_id),
            )
        definition.name = name
        definition.description = description
        definition.sort_order = int(command.sort_order)
        definition.is_active = bool(command.is_active)
        if created:
            db.add(definition)
        db.flush()
        emit_event(
            db,
            EventType.customer_connection_type_changed,
            {
                "connection_type_id": str(definition.id),
                "connection_type_code": definition.code,
                "change": "created" if created else "updated",
                "is_active": definition.is_active,
                "command_id": str(command.context.command_id),
            },
            actor=command.context.actor,
        )
        return ConnectionTypeOutcome(
            definition_id=definition.id,
            code=definition.code,
            name=definition.name,
            is_active=definition.is_active,
            created=created,
        )

    try:
        return execute_owner_command(
            db,
            definition=_SAVE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "duplicate_type",
            "A customer connection type with this name already exists.",
            field="name",
        ) from exc


def disable_definition(
    db: Session, command: DisableConnectionTypeCommand
) -> ConnectionTypeOutcome:
    """Deactivate a definition while retaining existing customer assignments."""

    def operation() -> ConnectionTypeOutcome:
        definition = db.scalar(
            select(CustomerConnectionTypeDefinition)
            .where(CustomerConnectionTypeDefinition.id == command.definition_id)
            .with_for_update()
        )
        if definition is None:
            raise _error(
                "type_not_found",
                "Customer connection type not found.",
                definition_id=str(command.definition_id),
            )
        replayed = not definition.is_active
        definition.is_active = False
        db.flush()
        if not replayed:
            emit_event(
                db,
                EventType.customer_connection_type_changed,
                {
                    "connection_type_id": str(definition.id),
                    "connection_type_code": definition.code,
                    "change": "disabled",
                    "is_active": False,
                    "command_id": str(command.context.command_id),
                },
                actor=command.context.actor,
            )
        return ConnectionTypeOutcome(
            definition_id=definition.id,
            code=definition.code,
            name=definition.name,
            is_active=False,
            created=False,
        )

    return execute_owner_command(
        db,
        definition=_DISABLE_DEFINITION,
        context=command.context,
        operation=operation,
    )
