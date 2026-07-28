"""Canonical owner for subscriber role and direct-permission assignments."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.rbac import Permission, Role, SubscriberPermission, SubscriberRole
from app.models.subscriber import Subscriber
from app.services import auth_cache
from app.services.audit_adapter import stage_audit_event
from app.services.common import apply_ordering, apply_pagination
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.response import list_response
from app.services.session_hooks import run_after_commit

ASSIGNMENT_SCOPE = "rbac:assign"
ALLOWED_SCOPE_TYPES = frozenset({"region", "reseller"})

_ASSIGNMENT_COMMAND = OwnerCommandDefinition(
    owner="auth.subscriber_assignments",
    concern="subscriber role and direct-permission assignments",
    name="change_subscriber_assignments",
)


class SubscriberAssignmentError(DomainError):
    """Stable, transport-neutral subscriber assignment failure."""


@dataclass(frozen=True)
class GrantSubscriberRoleCommand:
    context: CommandContext
    subscriber_id: UUID
    role_id: UUID
    scope_type: str = ""
    scope_id: str = ""


@dataclass(frozen=True)
class UpdateSubscriberRoleCommand:
    context: CommandContext
    grant_id: UUID
    subscriber_id: UUID | None = None
    role_id: UUID | None = None
    scope_type: str | None = None
    scope_id: str | None = None


@dataclass(frozen=True)
class RevokeSubscriberRoleCommand:
    context: CommandContext
    grant_id: UUID


@dataclass(frozen=True)
class GrantSubscriberPermissionCommand:
    context: CommandContext
    subscriber_id: UUID
    permission_id: UUID
    granted_by_subscriber_id: UUID | None = None


@dataclass(frozen=True)
class UpdateSubscriberPermissionCommand:
    context: CommandContext
    grant_id: UUID
    subscriber_id: UUID | None = None
    permission_id: UUID | None = None
    granted_by_subscriber_id: UUID | None = None
    update_granted_by: bool = False


@dataclass(frozen=True)
class RevokeSubscriberPermissionCommand:
    context: CommandContext
    grant_id: UUID


@dataclass(frozen=True)
class SubscriberRoleGrantSpec:
    role_id: UUID
    scope_type: str = ""
    scope_id: str = ""


@dataclass(frozen=True)
class ReplaceSubscriberAssignmentsCommand:
    context: CommandContext
    subscriber_id: UUID
    role_grants: tuple[SubscriberRoleGrantSpec, ...]
    direct_permission_ids: tuple[UUID, ...]
    granted_by_subscriber_id: UUID | None = None


@dataclass(frozen=True)
class SubscriberRoleOutcome:
    id: UUID
    subscriber_id: UUID
    role_id: UUID
    scope_type: str
    scope_id: str
    assigned_at: datetime
    changed: bool
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class SubscriberPermissionOutcome:
    id: UUID
    subscriber_id: UUID
    permission_id: UUID
    granted_at: datetime
    granted_by_subscriber_id: UUID | None
    changed: bool
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class SubscriberAssignmentsOutcome:
    subscriber_id: UUID
    role_grants: tuple[SubscriberRoleGrantSpec, ...]
    direct_permission_keys: tuple[str, ...]
    changed: bool
    command_id: UUID
    correlation_id: UUID


def _error(code: str, message: str, **details: object) -> SubscriberAssignmentError:
    return SubscriberAssignmentError(
        code=f"auth.subscriber_assignments.{code}",
        message=message,
        details=details,
    )


def _validate_context(context: CommandContext) -> tuple[AuditActorType, str]:
    if context.scope != ASSIGNMENT_SCOPE:
        raise _error(
            "invalid_command",
            "Subscriber assignment requires RBAC assignment authorization.",
            field="scope",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Subscriber assignment actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "Subscriber assignment actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _normalize_scope(scope_type: str, scope_id: str) -> tuple[str, str]:
    normalized_type = scope_type.strip().lower()
    normalized_id = scope_id.strip()
    if not normalized_type and not normalized_id:
        return "", ""
    if normalized_type not in ALLOWED_SCOPE_TYPES or not normalized_id:
        raise _error(
            "invalid_scope",
            "A scoped role grant requires a region or reseller scope and an identifier.",
            scope_type=normalized_type,
        )
    if len(normalized_id) > 64:
        raise _error(
            "invalid_scope",
            "Role grant scope identifier cannot exceed 64 characters.",
            field="scope_id",
        )
    return normalized_type, normalized_id


def _locked_subscriber(db: Session, subscriber_id: UUID) -> Subscriber:
    subscriber = db.execute(
        select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
    ).scalar_one_or_none()
    if subscriber is None:
        raise _error(
            "subscriber_not_found",
            "Subscriber was not found.",
            subscriber_id=str(subscriber_id),
        )
    return subscriber


def _locked_role(db: Session, role_id: UUID) -> Role:
    role = db.execute(
        select(Role).where(Role.id == role_id).with_for_update()
    ).scalar_one_or_none()
    if role is None or not role.is_active:
        raise _error(
            "role_not_found",
            "Active role was not found.",
            role_id=str(role_id),
        )
    return role


def _locked_permission(db: Session, permission_id: UUID) -> Permission:
    permission = db.execute(
        select(Permission).where(Permission.id == permission_id).with_for_update()
    ).scalar_one_or_none()
    if (
        permission is None
        or not permission.is_active
        or not permission.is_ui_assignable
    ):
        raise _error(
            "permission_not_found",
            "Active assignable permission was not found.",
            permission_id=str(permission_id),
        )
    return permission


def _flush_or_conflict(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        raise _error(
            "assignment_conflict",
            "Subscriber assignment conflicts with current canonical state.",
        ) from exc


def subscriber_role_specs(
    db: Session, subscriber_id: UUID
) -> tuple[SubscriberRoleGrantSpec, ...]:
    rows = tuple(
        db.execute(
            select(SubscriberRole)
            .where(SubscriberRole.subscriber_id == subscriber_id)
            .order_by(
                SubscriberRole.scope_type,
                SubscriberRole.scope_id,
                SubscriberRole.role_id,
            )
        ).scalars()
    )
    return tuple(
        SubscriberRoleGrantSpec(
            role_id=row.role_id,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
        )
        for row in rows
    )


def subscriber_direct_permission_keys(
    db: Session, subscriber_id: UUID
) -> tuple[str, ...]:
    return tuple(
        db.execute(
            select(Permission.key)
            .join(
                SubscriberPermission,
                SubscriberPermission.permission_id == Permission.id,
            )
            .where(
                SubscriberPermission.subscriber_id == subscriber_id,
                Permission.is_active.is_(True),
            )
            .distinct()
            .order_by(Permission.key)
        ).scalars()
    )


def _metadata(context: CommandContext, **values: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "causation_id": str(context.causation_id) if context.causation_id else None,
        "idempotency_key_sha256": (
            hashlib.sha256(context.idempotency_key.encode()).hexdigest()
            if context.idempotency_key
            else None
        ),
        "scope": context.scope,
        "reason": context.reason,
        **values,
    }


def _stage_change(
    db: Session,
    *,
    context: CommandContext,
    actor_type: AuditActorType,
    actor_id: str,
    subscriber_id: UUID,
    affected_subscriber_ids: set[UUID],
    changed: bool,
    operation: str,
) -> None:
    role_grants = subscriber_role_specs(db, subscriber_id)
    permission_keys = subscriber_direct_permission_keys(db, subscriber_id)
    metadata = _metadata(
        context,
        operation=operation,
        changed=changed,
        role_grants=[
            {
                "role_id": str(grant.role_id),
                "scope_type": grant.scope_type,
                "scope_id": grant.scope_id,
            }
            for grant in role_grants
        ],
        direct_permission_keys=list(permission_keys),
        affected_subscriber_ids=[
            str(value) for value in sorted(affected_subscriber_ids, key=str)
        ],
    )
    stage_audit_event(
        db,
        action=f"auth.subscriber_assignments.{operation}",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=str(context.correlation_id),
        metadata=metadata,
    )
    if changed:
        emit_event(
            db,
            EventType.subscriber_assignments_changed,
            {
                **metadata,
                "aggregate_type": "subscriber",
                "aggregate_id": str(subscriber_id),
                "aggregate_version": str(context.command_id),
                "subscriber_id": str(subscriber_id),
            },
            actor=context.actor,
            subscriber_id=subscriber_id,
        )

        def invalidate(_callback_db: Session) -> None:
            for affected_id in affected_subscriber_ids:
                auth_cache.invalidate_principal("subscriber", str(affected_id))

        run_after_commit(db, invalidate)


def _role_outcome(
    grant: SubscriberRole, context: CommandContext, *, changed: bool
) -> SubscriberRoleOutcome:
    return SubscriberRoleOutcome(
        id=grant.id,
        subscriber_id=grant.subscriber_id,
        role_id=grant.role_id,
        scope_type=grant.scope_type,
        scope_id=grant.scope_id,
        assigned_at=grant.assigned_at,
        changed=changed,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _permission_outcome(
    grant: SubscriberPermission, context: CommandContext, *, changed: bool
) -> SubscriberPermissionOutcome:
    return SubscriberPermissionOutcome(
        id=grant.id,
        subscriber_id=grant.subscriber_id,
        permission_id=grant.permission_id,
        granted_at=grant.granted_at,
        granted_by_subscriber_id=grant.granted_by_subscriber_id,
        changed=changed,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _ensure_role_grant(
    db: Session,
    *,
    subscriber_id: UUID,
    role_id: UUID,
    scope_type: str,
    scope_id: str,
) -> tuple[SubscriberRole, bool]:
    _locked_subscriber(db, subscriber_id)
    _locked_role(db, role_id)
    normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
    existing = db.execute(
        select(SubscriberRole)
        .where(
            SubscriberRole.subscriber_id == subscriber_id,
            SubscriberRole.role_id == role_id,
            SubscriberRole.scope_type == normalized_type,
            SubscriberRole.scope_id == normalized_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    grant = SubscriberRole(
        subscriber_id=subscriber_id,
        role_id=role_id,
        scope_type=normalized_type,
        scope_id=normalized_id,
    )
    db.add(grant)
    _flush_or_conflict(db)
    return grant, True


def grant_subscriber_role(
    db: Session, command: GrantSubscriberRoleCommand
) -> SubscriberRoleOutcome:
    """Grant one role in a manifest-verified owner transaction."""

    def operation() -> SubscriberRoleOutcome:
        actor_type, actor_id = _validate_context(command.context)
        grant, changed = _ensure_role_grant(
            db,
            subscriber_id=command.subscriber_id,
            role_id=command.role_id,
            scope_type=command.scope_type,
            scope_id=command.scope_id,
        )
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            subscriber_id=grant.subscriber_id,
            affected_subscriber_ids={grant.subscriber_id},
            changed=changed,
            operation="role_granted",
        )
        return _role_outcome(grant, command.context, changed=changed)

    return execute_owner_command(
        db,
        definition=_ASSIGNMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def update_subscriber_role(
    db: Session, command: UpdateSubscriberRoleCommand
) -> SubscriberRoleOutcome:
    """Update one role grant in a manifest-verified owner transaction."""

    def operation() -> SubscriberRoleOutcome:
        actor_type, actor_id = _validate_context(command.context)
        grant = db.execute(
            select(SubscriberRole)
            .where(SubscriberRole.id == command.grant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if grant is None:
            raise _error(
                "role_grant_not_found",
                "Subscriber role grant was not found.",
                grant_id=str(command.grant_id),
            )
        original_subscriber_id = grant.subscriber_id
        subscriber_id = command.subscriber_id or grant.subscriber_id
        role_id = command.role_id or grant.role_id
        scope_type = (
            command.scope_type if command.scope_type is not None else grant.scope_type
        )
        scope_id = command.scope_id if command.scope_id is not None else grant.scope_id
        _locked_subscriber(db, subscriber_id)
        _locked_role(db, role_id)
        normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
        changed = (
            subscriber_id != grant.subscriber_id
            or role_id != grant.role_id
            or normalized_type != grant.scope_type
            or normalized_id != grant.scope_id
        )
        grant.subscriber_id = subscriber_id
        grant.role_id = role_id
        grant.scope_type = normalized_type
        grant.scope_id = normalized_id
        _flush_or_conflict(db)
        affected = {original_subscriber_id, subscriber_id}
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            subscriber_id=subscriber_id,
            affected_subscriber_ids=affected,
            changed=changed,
            operation="role_updated",
        )
        return _role_outcome(grant, command.context, changed=changed)

    return execute_owner_command(
        db,
        definition=_ASSIGNMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def revoke_subscriber_role(db: Session, command: RevokeSubscriberRoleCommand) -> None:
    """Revoke one role grant in a manifest-verified owner transaction."""

    def operation() -> None:
        actor_type, actor_id = _validate_context(command.context)
        grant = db.execute(
            select(SubscriberRole)
            .where(SubscriberRole.id == command.grant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if grant is None:
            raise _error(
                "role_grant_not_found",
                "Subscriber role grant was not found.",
                grant_id=str(command.grant_id),
            )
        subscriber_id = grant.subscriber_id
        db.delete(grant)
        db.flush()
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            subscriber_id=subscriber_id,
            affected_subscriber_ids={subscriber_id},
            changed=True,
            operation="role_revoked",
        )

    execute_owner_command(
        db,
        definition=_ASSIGNMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def _ensure_permission_grant(
    db: Session,
    *,
    subscriber_id: UUID,
    permission_id: UUID,
    granted_by_subscriber_id: UUID | None,
) -> tuple[SubscriberPermission, bool]:
    _locked_subscriber(db, subscriber_id)
    _locked_permission(db, permission_id)
    if granted_by_subscriber_id is not None:
        _locked_subscriber(db, granted_by_subscriber_id)
    existing = db.execute(
        select(SubscriberPermission)
        .where(
            SubscriberPermission.subscriber_id == subscriber_id,
            SubscriberPermission.permission_id == permission_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    grant = SubscriberPermission(
        subscriber_id=subscriber_id,
        permission_id=permission_id,
        granted_by_subscriber_id=granted_by_subscriber_id,
    )
    db.add(grant)
    _flush_or_conflict(db)
    return grant, True


def grant_subscriber_permission(
    db: Session, command: GrantSubscriberPermissionCommand
) -> SubscriberPermissionOutcome:
    """Grant one direct permission in an owner transaction."""

    def operation() -> SubscriberPermissionOutcome:
        actor_type, actor_id = _validate_context(command.context)
        grant, changed = _ensure_permission_grant(
            db,
            subscriber_id=command.subscriber_id,
            permission_id=command.permission_id,
            granted_by_subscriber_id=command.granted_by_subscriber_id,
        )
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            subscriber_id=grant.subscriber_id,
            affected_subscriber_ids={grant.subscriber_id},
            changed=changed,
            operation="permission_granted",
        )
        return _permission_outcome(grant, command.context, changed=changed)

    return execute_owner_command(
        db,
        definition=_ASSIGNMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def update_subscriber_permission(
    db: Session, command: UpdateSubscriberPermissionCommand
) -> SubscriberPermissionOutcome:
    """Update one direct permission grant in an owner transaction."""

    def operation() -> SubscriberPermissionOutcome:
        actor_type, actor_id = _validate_context(command.context)
        grant = db.execute(
            select(SubscriberPermission)
            .where(SubscriberPermission.id == command.grant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if grant is None:
            raise _error(
                "permission_grant_not_found",
                "Subscriber permission grant was not found.",
                grant_id=str(command.grant_id),
            )
        original_subscriber_id = grant.subscriber_id
        subscriber_id = command.subscriber_id or grant.subscriber_id
        permission_id = command.permission_id or grant.permission_id
        granted_by = (
            command.granted_by_subscriber_id
            if command.update_granted_by
            else grant.granted_by_subscriber_id
        )
        _locked_subscriber(db, subscriber_id)
        _locked_permission(db, permission_id)
        if granted_by is not None:
            _locked_subscriber(db, granted_by)
        changed = (
            subscriber_id != grant.subscriber_id
            or permission_id != grant.permission_id
            or granted_by != grant.granted_by_subscriber_id
        )
        grant.subscriber_id = subscriber_id
        grant.permission_id = permission_id
        grant.granted_by_subscriber_id = granted_by
        _flush_or_conflict(db)
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            subscriber_id=subscriber_id,
            affected_subscriber_ids={original_subscriber_id, subscriber_id},
            changed=changed,
            operation="permission_updated",
        )
        return _permission_outcome(grant, command.context, changed=changed)

    return execute_owner_command(
        db,
        definition=_ASSIGNMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def revoke_subscriber_permission(
    db: Session, command: RevokeSubscriberPermissionCommand
) -> None:
    """Revoke one direct permission grant in an owner transaction."""

    def operation() -> None:
        actor_type, actor_id = _validate_context(command.context)
        grant = db.execute(
            select(SubscriberPermission)
            .where(SubscriberPermission.id == command.grant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if grant is None:
            raise _error(
                "permission_grant_not_found",
                "Subscriber permission grant was not found.",
                grant_id=str(command.grant_id),
            )
        subscriber_id = grant.subscriber_id
        db.delete(grant)
        db.flush()
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            subscriber_id=subscriber_id,
            affected_subscriber_ids={subscriber_id},
            changed=True,
            operation="permission_revoked",
        )

    execute_owner_command(
        db,
        definition=_ASSIGNMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def replace_subscriber_assignments(
    db: Session, command: ReplaceSubscriberAssignmentsCommand
) -> SubscriberAssignmentsOutcome:
    """Replace all role and direct-permission grants for one subscriber."""

    def operation() -> SubscriberAssignmentsOutcome:
        actor_type, actor_id = _validate_context(command.context)
        _locked_subscriber(db, command.subscriber_id)
        if len(command.role_grants) > 100 or len(command.direct_permission_ids) > 500:
            raise _error(
                "invalid_command",
                "Assignment command exceeds the supported cardinality.",
            )
        desired_roles: dict[tuple[UUID, str, str], SubscriberRoleGrantSpec] = {}
        for spec in command.role_grants:
            scope_type, scope_id = _normalize_scope(spec.scope_type, spec.scope_id)
            _locked_role(db, spec.role_id)
            normalized = SubscriberRoleGrantSpec(spec.role_id, scope_type, scope_id)
            desired_roles[(spec.role_id, scope_type, scope_id)] = normalized
        if len(desired_roles) != len(command.role_grants):
            raise _error("invalid_command", "Duplicate role grants are not allowed.")

        desired_permission_ids = set(command.direct_permission_ids)
        if len(desired_permission_ids) != len(command.direct_permission_ids):
            raise _error(
                "invalid_command", "Duplicate direct permissions are not allowed."
            )
        for permission_id in desired_permission_ids:
            _locked_permission(db, permission_id)
        if command.granted_by_subscriber_id is not None:
            _locked_subscriber(db, command.granted_by_subscriber_id)

        existing_roles = tuple(
            db.execute(
                select(SubscriberRole)
                .where(SubscriberRole.subscriber_id == command.subscriber_id)
                .with_for_update()
            ).scalars()
        )
        existing_role_keys = {
            (role_grant.role_id, role_grant.scope_type, role_grant.scope_id): role_grant
            for role_grant in existing_roles
        }
        changed = False
        for key, role_grant in existing_role_keys.items():
            if key not in desired_roles:
                db.delete(role_grant)
                changed = True
        for key, spec in desired_roles.items():
            if key not in existing_role_keys:
                db.add(
                    SubscriberRole(
                        subscriber_id=command.subscriber_id,
                        role_id=spec.role_id,
                        scope_type=spec.scope_type,
                        scope_id=spec.scope_id,
                    )
                )
                changed = True

        existing_permissions = tuple(
            db.execute(
                select(SubscriberPermission)
                .where(SubscriberPermission.subscriber_id == command.subscriber_id)
                .with_for_update()
            ).scalars()
        )
        existing_permission_ids = {
            permission_grant.permission_id: permission_grant
            for permission_grant in existing_permissions
        }
        for permission_id, permission_grant in existing_permission_ids.items():
            if permission_id not in desired_permission_ids:
                db.delete(permission_grant)
                changed = True
        for permission_id in desired_permission_ids - set(existing_permission_ids):
            db.add(
                SubscriberPermission(
                    subscriber_id=command.subscriber_id,
                    permission_id=permission_id,
                    granted_by_subscriber_id=command.granted_by_subscriber_id,
                )
            )
            changed = True
        _flush_or_conflict(db)
        _stage_change(
            db,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            subscriber_id=command.subscriber_id,
            affected_subscriber_ids={command.subscriber_id},
            changed=changed,
            operation="replaced",
        )
        return SubscriberAssignmentsOutcome(
            subscriber_id=command.subscriber_id,
            role_grants=subscriber_role_specs(db, command.subscriber_id),
            direct_permission_keys=subscriber_direct_permission_keys(
                db, command.subscriber_id
            ),
            changed=changed,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_ASSIGNMENT_COMMAND,
        context=command.context,
        operation=operation,
    )


def ensure_role_grant_in_transaction(
    db: Session,
    *,
    context: CommandContext,
    subscriber_id: UUID,
    role_id: UUID,
    scope_type: str = "",
    scope_id: str = "",
) -> SubscriberRole:
    """Grant a role inside an authorized application-coordinator transaction."""

    actor_type, actor_id = _validate_context(context)
    grant, changed = _ensure_role_grant(
        db,
        subscriber_id=subscriber_id,
        role_id=role_id,
        scope_type=scope_type,
        scope_id=scope_id,
    )
    _stage_change(
        db,
        context=context,
        actor_type=actor_type,
        actor_id=actor_id,
        subscriber_id=subscriber_id,
        affected_subscriber_ids={subscriber_id},
        changed=changed,
        operation="role_granted",
    )
    return grant


def ensure_seeded_role_grant(
    db: Session,
    *,
    subscriber_id: UUID,
    role_id: UUID,
) -> SubscriberRole:
    """Flush-only bootstrap collaborator; the seed coordinator owns commit."""

    grant, _changed = _ensure_role_grant(
        db,
        subscriber_id=subscriber_id,
        role_id=role_id,
        scope_type="",
        scope_id="",
    )
    return grant


def get_subscriber_role(db: Session, grant_id: UUID) -> SubscriberRole | None:
    return db.get(SubscriberRole, grant_id)


def list_subscriber_roles(
    db: Session,
    *,
    subscriber_id: UUID | None,
    role_id: UUID | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
) -> list[SubscriberRole]:
    query = db.query(SubscriberRole)
    if subscriber_id is not None:
        query = query.filter(SubscriberRole.subscriber_id == subscriber_id)
    if role_id is not None:
        query = query.filter(SubscriberRole.role_id == role_id)
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"assigned_at": SubscriberRole.assigned_at},
    )
    return apply_pagination(query, limit, offset).all()


def list_subscriber_roles_response(
    db: Session,
    *,
    subscriber_id: UUID | None,
    role_id: UUID | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
) -> dict[str, object]:
    return list_response(
        list_subscriber_roles(
            db,
            subscriber_id=subscriber_id,
            role_id=role_id,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            offset=offset,
        ),
        limit,
        offset,
    )


def get_subscriber_permission(
    db: Session, grant_id: UUID
) -> SubscriberPermission | None:
    return db.get(SubscriberPermission, grant_id)


def list_subscriber_permissions(
    db: Session,
    *,
    subscriber_id: UUID | None,
    permission_id: UUID | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
) -> list[SubscriberPermission]:
    query = db.query(SubscriberPermission)
    if subscriber_id is not None:
        query = query.filter(SubscriberPermission.subscriber_id == subscriber_id)
    if permission_id is not None:
        query = query.filter(SubscriberPermission.permission_id == permission_id)
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"granted_at": SubscriberPermission.granted_at},
    )
    return apply_pagination(query, limit, offset).all()
