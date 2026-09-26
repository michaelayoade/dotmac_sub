"""Canonical lifecycle and value owner for centrally governed custom fields."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.custom_fields import (
    CustomFieldDefinition,
    CustomFieldDefinitionStatus,
    CustomFieldType,
    CustomFieldValue,
)
from app.services import custom_field_capabilities, custom_field_targets
from app.services.audit_adapter import stage_audit_event
from app.services.custom_field_permissions import permission_granted
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "custom_fields.records"
HUB_READ_PERMISSION = "custom_fields:hub:read"
DEFINITION_READ_PERMISSION = "custom_fields:definition:read"
DEFINITION_CREATE_PERMISSION = "custom_fields:definition:create"
DEFINITION_UPDATE_PERMISSION = "custom_fields:definition:update"
DEFINITION_ACTIVATE_PERMISSION = "custom_fields:definition:activate"
DEFINITION_RETIRE_PERMISSION = "custom_fields:definition:retire"
VALUE_READ_PERMISSION = "custom_fields:value:read"
VALUE_WRITE_PERMISSION = "custom_fields:value:write"
SENSITIVE_READ_PERMISSION = "custom_fields:sensitive:read"
SENSITIVE_WRITE_PERMISSION = "custom_fields:sensitive:write"

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CREATE = OwnerCommandDefinition(
    owner=OWNER,
    concern="custom-field definitions and typed entity values",
    name="create_custom_field_definition",
)
_UPDATE = OwnerCommandDefinition(
    owner=OWNER,
    concern="custom-field definitions and typed entity values",
    name="update_custom_field_definition",
)
_CHANGE_STATUS = OwnerCommandDefinition(
    owner=OWNER,
    concern="custom-field definitions and typed entity values",
    name="change_custom_field_definition_status",
)
_SET_VALUE = OwnerCommandDefinition(
    owner=OWNER,
    concern="custom-field definitions and typed entity values",
    name="set_custom_field_value",
)


class CustomFieldError(DomainError):
    pass


class CustomFieldDefinitionOperation(StrEnum):
    activate = "activate"
    retire = "retire"


@dataclass(frozen=True, slots=True)
class CustomFieldValidation:
    min_length: int | None = None
    max_length: int | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    pattern: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class CustomFieldDefinitionInput:
    label: str
    description: str | None
    field_type: CustomFieldType
    options: tuple[str, ...] = ()
    validation: CustomFieldValidation = CustomFieldValidation()
    default_value: object | None = None
    required: bool = False
    sensitive: bool = False
    section: str = "Additional information"
    display_order: int = 0
    show_in_list: bool = False
    show_in_form: bool = True
    show_in_detail: bool = True


@dataclass(frozen=True, slots=True)
class CreateCustomFieldDefinitionCommand:
    tenant_id: UUID
    target_type: str
    key: str
    definition: CustomFieldDefinitionInput
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class UpdateCustomFieldDefinitionCommand:
    tenant_id: UUID
    definition_id: UUID
    definition: CustomFieldDefinitionInput
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class ChangeCustomFieldDefinitionStatusCommand:
    tenant_id: UUID
    definition_id: UUID
    operation: CustomFieldDefinitionOperation
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class SetCustomFieldValueCommand:
    tenant_id: UUID
    definition_id: UUID
    target_type: str
    target_id: UUID
    value: object | None
    permission_keys: frozenset[str]
    context: CommandContext


@dataclass(frozen=True, slots=True)
class CustomFieldDefinitionOutcome:
    definition_id: UUID
    status: CustomFieldDefinitionStatus


@dataclass(frozen=True, slots=True)
class CustomFieldValueOutcome:
    definition_id: UUID
    target_id: UUID
    cleared: bool


@dataclass(frozen=True, slots=True)
class CustomFieldValueProjection:
    definition: CustomFieldDefinition
    value: object | None
    redacted: bool


def _error(code: str, message: str, **details: object) -> CustomFieldError:
    return CustomFieldError(code=f"{OWNER}.{code}", message=message, details=details)


def _has(permission_keys: frozenset[str], permission: str) -> bool:
    return permission_granted(permission_keys, permission)


def _require(permission_keys: frozenset[str], *permissions: str) -> None:
    missing = [
        permission
        for permission in permissions
        if not _has(permission_keys, permission)
    ]
    if missing:
        raise _error(
            "permission_denied",
            "The custom-field operation is not authorized.",
            required_permissions=missing,
        )


def _target(target_type: str):
    try:
        return custom_field_capabilities.target_capability(target_type)
    except custom_field_capabilities.CustomFieldCapabilityError as exc:
        raise _error(
            "target_undeclared", "The selected module target is not registered."
        ) from exc


def _validation_json(value: CustomFieldValidation) -> dict[str, object]:
    result: dict[str, object] = {}
    if value.min_length is not None:
        result["min_length"] = value.min_length
    if value.max_length is not None:
        result["max_length"] = value.max_length
    if value.minimum is not None:
        result["minimum"] = str(value.minimum)
    if value.maximum is not None:
        result["maximum"] = str(value.maximum)
    if value.pattern:
        result["pattern"] = value.pattern
    if value.message:
        result["message"] = value.message.strip()
    return result


def _normalize_options(
    field_type: CustomFieldType, values: tuple[str, ...]
) -> list[str]:
    options = [value.strip() for value in values if value.strip()]
    if len(options) != len(set(options)):
        raise _error("options_invalid", "Option values must be unique.")
    if field_type in {CustomFieldType.select, CustomFieldType.multiselect}:
        if not options:
            raise _error(
                "options_invalid", "Select fields require at least one option."
            )
    elif options:
        raise _error("options_invalid", "Only select fields can declare options.")
    return options


def _validate_constraints(validation: CustomFieldValidation) -> dict[str, object]:
    if validation.min_length is not None and validation.min_length < 0:
        raise _error("validation_invalid", "Minimum length cannot be negative.")
    if validation.max_length is not None and validation.max_length < 1:
        raise _error("validation_invalid", "Maximum length must be positive.")
    if (
        validation.min_length is not None
        and validation.max_length is not None
        and validation.min_length > validation.max_length
    ):
        raise _error("validation_invalid", "Minimum length exceeds maximum length.")
    if (
        validation.minimum is not None
        and validation.maximum is not None
        and validation.minimum > validation.maximum
    ):
        raise _error("validation_invalid", "Minimum value exceeds maximum value.")
    if validation.pattern:
        try:
            re.compile(validation.pattern)
        except re.error as exc:
            raise _error(
                "validation_invalid", "Validation pattern is invalid."
            ) from exc
    return _validation_json(validation)


def _definition_shape(
    value: CustomFieldDefinitionInput,
) -> tuple[str, str | None, list[str], dict[str, object], object | None, str]:
    label = value.label.strip()
    section = value.section.strip()
    if not label or not section or value.display_order < 0:
        raise _error(
            "definition_invalid", "Label, section, and display order are invalid."
        )
    options = _normalize_options(value.field_type, value.options)
    validation = _validate_constraints(value.validation)
    default_value = _normalize_value_parts(
        field_type=value.field_type,
        options=options,
        validation=validation,
        required=False,
        raw=value.default_value,
    )
    return (
        label,
        (value.description or "").strip() or None,
        options,
        validation,
        default_value,
        section,
    )


def _as_decimal(raw: object) -> Decimal:
    if isinstance(raw, bool):
        raise _error("value_invalid", "Enter a valid number.")
    try:
        value = Decimal(str(raw))
        if not value.is_finite():
            raise InvalidOperation
        return value
    except (InvalidOperation, ValueError) as exc:
        raise _error("value_invalid", "Enter a valid number.") from exc


def _normalize_value_parts(
    *,
    field_type: CustomFieldType,
    options: list[str],
    validation: dict[str, object],
    required: bool,
    raw: object | None,
) -> object | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise _error("value_required", "This custom field is required.")
        return None
    try:
        if field_type is CustomFieldType.boolean:
            if isinstance(raw, bool):
                value: object = raw
            elif str(raw).strip().casefold() in {"true", "1", "yes", "on"}:
                value = True
            elif str(raw).strip().casefold() in {"false", "0", "no", "off"}:
                value = False
            else:
                raise _error("value_invalid", "Enter true or false.")
        elif field_type is CustomFieldType.integer:
            if isinstance(raw, bool) or str(raw).strip() != str(int(str(raw).strip())):
                raise ValueError
            value = int(str(raw).strip())
        elif field_type in {CustomFieldType.decimal, CustomFieldType.currency}:
            value = str(_as_decimal(raw))
        elif field_type is CustomFieldType.date:
            value = date.fromisoformat(str(raw).strip()).isoformat()
        elif field_type is CustomFieldType.datetime:
            parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise _error("value_invalid", "Enter a datetime with a timezone.")
            value = parsed.isoformat()
        elif field_type is CustomFieldType.multiselect:
            supplied = (
                raw
                if isinstance(raw, list | tuple)
                else str(raw).strip().removeprefix("[").removesuffix("]").split(",")
            )
            selected = [
                str(item).strip().strip("'\"")
                for item in supplied
                if str(item).strip().strip("'\"")
            ]
            if required and not selected:
                raise _error("value_required", "This custom field is required.")
            if len(selected) != len(set(selected)) or any(
                item not in options for item in selected
            ):
                raise _error("value_invalid", "Choose only declared options.")
            value = selected
        else:
            if not isinstance(raw, str):
                raise _error("value_invalid", "Enter a text value.")
            value = str(raw).strip()
    except (ValueError, TypeError) as exc:
        raise _error(
            "value_invalid", f"Value is not valid for {field_type.value}."
        ) from exc

    if field_type is CustomFieldType.select and value not in options:
        raise _error("value_invalid", "Choose a declared option.")
    if field_type is CustomFieldType.email and not _EMAIL_PATTERN.fullmatch(str(value)):
        raise _error("value_invalid", "Enter a valid email address.")
    if field_type is CustomFieldType.url:
        parsed_url = urlparse(str(value))
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise _error("value_invalid", "Enter a valid HTTP or HTTPS URL.")
    if isinstance(value, str):
        minimum_length = validation.get("min_length")
        maximum_length = validation.get("max_length")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise _error("value_invalid", "Value is shorter than the allowed minimum.")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise _error("value_invalid", "Value exceeds the allowed maximum.")
        pattern = validation.get("pattern")
        if isinstance(pattern, str) and not re.fullmatch(pattern, value):
            raise _error(
                "value_invalid",
                str(
                    validation.get("message")
                    or "Value does not match the required format."
                ),
            )
    if field_type in {
        CustomFieldType.integer,
        CustomFieldType.decimal,
        CustomFieldType.currency,
    }:
        numeric = _as_decimal(value)
        minimum = validation.get("minimum")
        maximum = validation.get("maximum")
        if minimum is not None and numeric < Decimal(str(minimum)):
            raise _error("value_invalid", "Value is below the allowed minimum.")
        if maximum is not None and numeric > Decimal(str(maximum)):
            raise _error("value_invalid", "Value exceeds the allowed maximum.")
    return value


def _normalize_value(
    definition: CustomFieldDefinition, raw: object | None
) -> object | None:
    return _normalize_value_parts(
        field_type=CustomFieldType(definition.field_type),
        options=list(definition.options or []),
        validation=dict(definition.validation or {}),
        required=definition.required,
        raw=raw,
    )


def _definition(
    db: Session, *, tenant_id: UUID, definition_id: UUID, lock: bool = False
) -> CustomFieldDefinition:
    statement = select(CustomFieldDefinition).where(
        CustomFieldDefinition.tenant_id == tenant_id,
        CustomFieldDefinition.id == definition_id,
    )
    if lock:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        raise _error("not_found", "Custom-field definition not found.")
    return row


def _emit_definition_change(
    db: Session, definition: CustomFieldDefinition, change: str, context: CommandContext
) -> None:
    metadata = {
        "schema_version": 1,
        "tenant_id": str(definition.tenant_id),
        "definition_id": str(definition.id),
        "target_type": definition.target_type,
        "field_key": definition.key,
        "field_type": definition.field_type,
        "status": definition.status,
        "change": change,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
    }
    stage_audit_event(
        db,
        action="custom_field.definition_changed",
        entity_type="custom_field_definition",
        entity_id=str(definition.id),
        actor_id=context.actor,
        request_id=str(context.correlation_id),
        metadata=metadata,
    )
    emit_event(
        db, EventType.custom_field_definition_changed, metadata, actor=context.actor
    )


def create_definition(
    db: Session, command: CreateCustomFieldDefinitionCommand
) -> CustomFieldDefinitionOutcome:
    def operation() -> CustomFieldDefinitionOutcome:
        target = _target(command.target_type)
        _require(
            command.permission_keys,
            DEFINITION_CREATE_PERMISSION,
            target.write_permission,
        )
        if command.definition.sensitive:
            _require(command.permission_keys, SENSITIVE_WRITE_PERMISSION)
        key = command.key.strip()
        if not _KEY_PATTERN.fullmatch(key):
            raise _error(
                "definition_invalid",
                "Field key must start with a letter and use lowercase letters, numbers, or underscores.",
            )
        existing = db.scalar(
            select(CustomFieldDefinition).where(
                CustomFieldDefinition.tenant_id == command.tenant_id,
                CustomFieldDefinition.target_type == target.key,
                CustomFieldDefinition.key == key,
            )
        )
        if existing is not None:
            raise _error(
                "key_conflict", "This module already has a field with that key."
            )
        label, description, options, validation, default_value, section = (
            _definition_shape(command.definition)
        )
        row = CustomFieldDefinition(
            tenant_id=command.tenant_id,
            target_type=target.key,
            key=key,
            label=label,
            description=description,
            field_type=command.definition.field_type.value,
            options=options,
            validation=validation,
            default_value=default_value,
            required=command.definition.required,
            sensitive=command.definition.sensitive,
            section=section,
            display_order=command.definition.display_order,
            show_in_list=command.definition.show_in_list,
            show_in_form=command.definition.show_in_form,
            show_in_detail=command.definition.show_in_detail,
            created_by=command.context.actor,
            updated_by=command.context.actor,
        )
        db.add(row)
        db.flush()
        _emit_definition_change(db, row, "created", command.context)
        return CustomFieldDefinitionOutcome(row.id, CustomFieldDefinitionStatus.draft)

    return execute_owner_command(
        db, definition=_CREATE, context=command.context, operation=operation
    )


def update_definition(
    db: Session, command: UpdateCustomFieldDefinitionCommand
) -> CustomFieldDefinitionOutcome:
    def operation() -> CustomFieldDefinitionOutcome:
        _require(command.permission_keys, DEFINITION_UPDATE_PERMISSION)
        row = _definition(
            db,
            tenant_id=command.tenant_id,
            definition_id=command.definition_id,
            lock=True,
        )
        target = _target(row.target_type)
        _require(command.permission_keys, target.write_permission)
        if row.sensitive or command.definition.sensitive:
            _require(command.permission_keys, SENSITIVE_WRITE_PERMISSION)
        if row.status == CustomFieldDefinitionStatus.retired.value:
            raise _error("status_conflict", "A retired field cannot be changed.")
        label, description, options, validation, default_value, section = (
            _definition_shape(command.definition)
        )
        if row.status == CustomFieldDefinitionStatus.active.value:
            protected_shape = (
                command.definition.field_type.value,
                options,
                validation,
                default_value,
                command.definition.required,
                command.definition.sensitive,
            )
            current_shape = (
                row.field_type,
                list(row.options or []),
                dict(row.validation or {}),
                row.default_value,
                row.required,
                row.sensitive,
            )
            if protected_shape != current_shape:
                raise _error(
                    "active_definition_locked",
                    "Type, validation, options, default, required, and sensitivity are locked after activation.",
                )
        row.label = label
        row.description = description
        row.section = section
        row.display_order = command.definition.display_order
        row.show_in_list = command.definition.show_in_list
        row.show_in_form = command.definition.show_in_form
        row.show_in_detail = command.definition.show_in_detail
        if row.status == CustomFieldDefinitionStatus.draft.value:
            row.field_type = command.definition.field_type.value
            row.options = options
            row.validation = validation
            row.default_value = default_value
            row.required = command.definition.required
            row.sensitive = command.definition.sensitive
        row.updated_by = command.context.actor
        row.updated_at = datetime.now(UTC)
        db.flush()
        _emit_definition_change(db, row, "updated", command.context)
        return CustomFieldDefinitionOutcome(
            row.id, CustomFieldDefinitionStatus(row.status)
        )

    return execute_owner_command(
        db, definition=_UPDATE, context=command.context, operation=operation
    )


def change_definition_status(
    db: Session, command: ChangeCustomFieldDefinitionStatusCommand
) -> CustomFieldDefinitionOutcome:
    def operation() -> CustomFieldDefinitionOutcome:
        row = _definition(
            db,
            tenant_id=command.tenant_id,
            definition_id=command.definition_id,
            lock=True,
        )
        target = _target(row.target_type)
        required_permission = (
            DEFINITION_ACTIVATE_PERMISSION
            if command.operation is CustomFieldDefinitionOperation.activate
            else DEFINITION_RETIRE_PERMISSION
        )
        _require(command.permission_keys, required_permission, target.write_permission)
        if command.operation is CustomFieldDefinitionOperation.activate:
            if row.status == CustomFieldDefinitionStatus.active.value:
                return CustomFieldDefinitionOutcome(
                    row.id, CustomFieldDefinitionStatus.active
                )
            if row.status != CustomFieldDefinitionStatus.draft.value:
                raise _error("status_conflict", "Only a draft field can be activated.")
            active_count = int(
                db.scalar(
                    select(func.count(CustomFieldDefinition.id)).where(
                        CustomFieldDefinition.tenant_id == command.tenant_id,
                        CustomFieldDefinition.target_type == row.target_type,
                        CustomFieldDefinition.status
                        == CustomFieldDefinitionStatus.active.value,
                    )
                )
                or 0
            )
            if active_count >= target.maximum_active_fields:
                raise _error(
                    "active_limit_reached",
                    "This module has reached its active custom-field limit.",
                    limit=target.maximum_active_fields,
                )
            row.status = CustomFieldDefinitionStatus.active.value
            row.activated_at = datetime.now(UTC)
            change = "activated"
        else:
            if row.status == CustomFieldDefinitionStatus.retired.value:
                return CustomFieldDefinitionOutcome(
                    row.id, CustomFieldDefinitionStatus.retired
                )
            row.status = CustomFieldDefinitionStatus.retired.value
            row.retired_at = datetime.now(UTC)
            change = "retired"
        row.updated_by = command.context.actor
        row.updated_at = datetime.now(UTC)
        db.flush()
        _emit_definition_change(db, row, change, command.context)
        return CustomFieldDefinitionOutcome(
            row.id, CustomFieldDefinitionStatus(row.status)
        )

    return execute_owner_command(
        db, definition=_CHANGE_STATUS, context=command.context, operation=operation
    )


def set_value(
    db: Session, command: SetCustomFieldValueCommand
) -> CustomFieldValueOutcome:
    def operation() -> CustomFieldValueOutcome:
        target = _target(command.target_type)
        _require(
            command.permission_keys, VALUE_WRITE_PERMISSION, target.write_permission
        )
        row = _definition(
            db,
            tenant_id=command.tenant_id,
            definition_id=command.definition_id,
            lock=True,
        )
        if row.target_type != target.key:
            raise _error(
                "target_mismatch", "The field does not belong to this target type."
            )
        if row.status != CustomFieldDefinitionStatus.active.value:
            raise _error("status_conflict", "Only active custom fields accept values.")
        if row.sensitive:
            _require(command.permission_keys, SENSITIVE_WRITE_PERMISSION)
        if not custom_field_targets.target_exists(
            db, target_type=target.key, target_id=command.target_id
        ):
            raise _error("target_not_found", "The target record does not exist.")
        normalized = _normalize_value(row, command.value)
        stored = db.scalar(
            select(CustomFieldValue)
            .where(
                CustomFieldValue.tenant_id == command.tenant_id,
                CustomFieldValue.definition_id == row.id,
                CustomFieldValue.target_id == command.target_id,
            )
            .with_for_update()
        )
        cleared = normalized is None
        if cleared:
            if stored is not None:
                db.delete(stored)
        elif stored is None:
            stored = CustomFieldValue(
                tenant_id=command.tenant_id,
                definition_id=row.id,
                target_type=target.key,
                target_id=command.target_id,
                value=normalized,
                created_by=command.context.actor,
                updated_by=command.context.actor,
            )
            db.add(stored)
        else:
            stored.value = normalized
            stored.updated_by = command.context.actor
            stored.updated_at = datetime.now(UTC)
        db.flush()
        metadata = {
            "schema_version": 1,
            "tenant_id": str(command.tenant_id),
            "definition_id": str(row.id),
            "target_type": target.key,
            "target_id": str(command.target_id),
            "field_key": row.key,
            "change": "cleared" if cleared else "set",
            "sensitive": row.sensitive,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        }
        stage_audit_event(
            db,
            action="custom_field.value_changed",
            entity_type=target.key,
            entity_id=str(command.target_id),
            actor_id=command.context.actor,
            request_id=str(command.context.correlation_id),
            metadata=metadata,
        )
        emit_event(
            db,
            EventType.custom_field_value_changed,
            metadata,
            actor=command.context.actor,
            subscriber_id=command.target_id if target.key == "subscriber" else None,
        )
        return CustomFieldValueOutcome(row.id, command.target_id, cleared)

    return execute_owner_command(
        db, definition=_SET_VALUE, context=command.context, operation=operation
    )


def list_definitions(
    db: Session,
    *,
    tenant_id: UUID,
    target_type: str | None = None,
    include_retired: bool = False,
) -> tuple[CustomFieldDefinition, ...]:
    statement = select(CustomFieldDefinition).where(
        CustomFieldDefinition.tenant_id == tenant_id
    )
    if target_type:
        _target(target_type)
        statement = statement.where(CustomFieldDefinition.target_type == target_type)
    if not include_retired:
        statement = statement.where(
            CustomFieldDefinition.status != CustomFieldDefinitionStatus.retired.value
        )
    statement = statement.order_by(
        CustomFieldDefinition.target_type,
        CustomFieldDefinition.section,
        CustomFieldDefinition.display_order,
        CustomFieldDefinition.label,
    )
    return tuple(db.scalars(statement).all())


def list_target_values(
    db: Session,
    *,
    tenant_id: UUID,
    target_type: str,
    target_id: UUID,
    permission_keys: frozenset[str],
) -> tuple[CustomFieldValueProjection, ...]:
    target = _target(target_type)
    _require(permission_keys, VALUE_READ_PERMISSION, target.read_permission)
    if not custom_field_targets.target_exists(
        db, target_type=target.key, target_id=target_id
    ):
        raise _error("target_not_found", "The target record does not exist.")
    definitions = list_definitions(
        db, tenant_id=tenant_id, target_type=target_type, include_retired=False
    )
    values = {
        value.definition_id: value.value
        for value in db.scalars(
            select(CustomFieldValue).where(
                CustomFieldValue.tenant_id == tenant_id,
                CustomFieldValue.target_type == target_type,
                CustomFieldValue.target_id == target_id,
            )
        ).all()
    }
    can_read_sensitive = _has(permission_keys, SENSITIVE_READ_PERMISSION)
    return tuple(
        CustomFieldValueProjection(
            definition=definition,
            value=(
                values.get(definition.id)
                if not definition.sensitive or can_read_sensitive
                else None
            ),
            redacted=definition.sensitive and not can_read_sensitive,
        )
        for definition in definitions
        if definition.status == CustomFieldDefinitionStatus.active.value
    )


__all__ = [
    "ChangeCustomFieldDefinitionStatusCommand",
    "CreateCustomFieldDefinitionCommand",
    "CustomFieldDefinitionInput",
    "CustomFieldDefinitionOperation",
    "CustomFieldError",
    "CustomFieldValidation",
    "SetCustomFieldValueCommand",
    "UpdateCustomFieldDefinitionCommand",
    "change_definition_status",
    "create_definition",
    "list_definitions",
    "list_target_values",
    "set_value",
    "update_definition",
]
