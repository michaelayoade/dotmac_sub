"""Transport-neutral canonical permission calculation owned by auth.permission_gate.

Legacy HTTP wrappers and strict staff admission delegate to this calculator.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberPermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.services.permission_matching import _expand_permission_keys


class PermissionPrincipalKind(StrEnum):
    system_user = "system_user"
    subscriber = "subscriber"
    api_key = "api_key"
    reseller_user = "reseller_user"


@dataclass(frozen=True, slots=True)
class PermissionPrincipal:
    principal_id: UUID
    principal_type: PermissionPrincipalKind
    roles: frozenset[str]
    scopes: frozenset[str]


def has_permission(auth: PermissionPrincipal, db: Session, permission_key: str) -> bool:
    principal_id = auth.principal_id
    principal_type = auth.principal_type
    roles = set(auth.roles)
    if "admin" in roles:
        return True

    possible_keys = _expand_permission_keys(permission_key)

    scopes = set(auth.scopes)
    if scopes & set(possible_keys):
        return True

    permissions = (
        db.query(Permission)
        .filter(Permission.key.in_(possible_keys))
        .filter(Permission.is_active.is_(True))
        .all()
    )
    if not permissions:
        return False

    permission_ids = [p.id for p in permissions]

    has_role_permission: RolePermission | None = None
    has_direct_permission: SystemUserPermission | SubscriberPermission | None = None
    if principal_type == "system_user":
        has_role_permission = (
            db.query(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_id)
            .filter(RolePermission.permission_id.in_(permission_ids))
            .filter(Role.is_active.is_(True))
            .first()
        )
        has_direct_permission = (
            db.query(SystemUserPermission)
            .filter(SystemUserPermission.system_user_id == principal_id)
            .filter(SystemUserPermission.permission_id.in_(permission_ids))
            .first()
        )
    else:
        has_role_permission = (
            db.query(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_id)
            .filter(RolePermission.permission_id.in_(permission_ids))
            .filter(Role.is_active.is_(True))
            .first()
        )
        has_direct_permission = (
            db.query(SubscriberPermission)
            .filter(SubscriberPermission.subscriber_id == principal_id)
            .filter(SubscriberPermission.permission_id.in_(permission_ids))
            .first()
        )

    return bool(has_role_permission or has_direct_permission)


def effective_permission_keys(auth: PermissionPrincipal, db: Session) -> frozenset[str]:
    """All permission keys the principal effectively holds, for UI gating.

    Returns the raw granted keys (scopes + role + direct); the ``admin`` role
    collapses to the ``"*"`` sentinel meaning "everything". A requirement is
    checked by expanding it with ``_expand_permission_keys`` against this set
    (see ``can``), mirroring ``has_permission`` — so the UI can hide what the
    principal cannot do while the route stays the authority.
    """
    roles = set(auth.roles)
    if "admin" in roles:
        return frozenset({"*"})
    keys: set[str] = set(auth.scopes)
    principal_id = auth.principal_id
    principal_type = auth.principal_type
    if principal_type == "system_user":
        role_rows = (
            db.query(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_id)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
        )
        direct_rows = (
            db.query(Permission.key)
            .join(
                SystemUserPermission,
                SystemUserPermission.permission_id == Permission.id,
            )
            .filter(SystemUserPermission.system_user_id == principal_id)
            .filter(Permission.is_active.is_(True))
        )
    else:
        role_rows = (
            db.query(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_id)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
        )
        direct_rows = (
            db.query(Permission.key)
            .join(
                SubscriberPermission,
                SubscriberPermission.permission_id == Permission.id,
            )
            .filter(SubscriberPermission.subscriber_id == principal_id)
            .filter(Permission.is_active.is_(True))
        )
    keys.update(key for (key,) in role_rows.all())
    keys.update(key for (key,) in direct_rows.all())
    return frozenset(keys)


def system_user_holds_exact_permission(
    db: Session, system_user_id: UUID, permission_key: str
) -> bool:
    """Whether the system user was explicitly granted exactly ``permission_key``.

    For dedicated, separately granted permissions (for example the Starter
    ledger-live switch): the ``admin`` role bypass and wildcard grants such as
    ``*`` or ``billing:*`` deliberately do NOT satisfy it. Only an active
    permission row granted directly or through an active role counts, except
    for ``DIRECT_ONLY_PERMISSION_KEYS``, which only a direct grant satisfies.
    """
    permission_id = (
        db.query(Permission.id)
        .filter(Permission.key == permission_key)
        .filter(Permission.is_active.is_(True))
        .scalar()
    )
    if permission_id is None:
        return False
    if permission_key in DIRECT_ONLY_PERMISSION_KEYS:
        # Direct-only: role membership (including ERP HR / local staff sync)
        # never satisfies these keys; only an explicit direct user grant does.
        via_role = None
    else:
        via_role = _role_grant(db, system_user_id, permission_id)
    if via_role is not None:
        return True
    return (
        db.query(SystemUserPermission.id)
        .filter(SystemUserPermission.system_user_id == system_user_id)
        .filter(SystemUserPermission.permission_id == permission_id)
        .first()
        is not None
    )


def _role_grant(db: Session, system_user_id: UUID, permission_id: UUID):
    return (
        db.query(RolePermission.id)
        .join(Role, RolePermission.role_id == Role.id)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .filter(SystemUserRole.system_user_id == system_user_id)
        .filter(RolePermission.permission_id == permission_id)
        .filter(Role.is_active.is_(True))
        .first()
    )


#: Permissions that only an acting principal holding the ``admin`` role may
#: grant or revoke (directly to a user or by adding them to a role). They are
#: also granted to no role by default, so holding ``admin`` alone never
#: satisfies them. Kept in step with ``SettingSpec.required_permission``
#: (tests/test_starter_ledger_live_permission.py).
ADMIN_ONLY_GRANT_PERMISSION_KEYS = frozenset({"billing:starter_ledger_live:write"})

#: Permissions that exist only as DIRECT user grants. They can never be put on a
#: role (``rbac_catalog`` refuses, even for an admin) and a role never satisfies
#: them in ``system_user_holds_exact_permission``, so role membership - ERP HR
#: sync, local staff provisioning, role reactivation - cannot confer them.
DIRECT_ONLY_PERMISSION_KEYS = frozenset({"billing:starter_ledger_live:write"})


def actor_holds_admin_role(db: Session, actor: str) -> bool:
    """Whether ``user:<system user id>`` holds the active ``admin`` role."""
    prefix, _, identifier = actor.partition(":")
    if prefix != "user":
        return False
    try:
        user_id = UUID(identifier)
    except ValueError:
        return False
    return (
        db.query(SystemUserRole.id)
        .join(Role, SystemUserRole.role_id == Role.id)
        .filter(SystemUserRole.system_user_id == user_id)
        .filter(Role.name == "admin")
        .filter(Role.is_active.is_(True))
        .first()
        is not None
    )


def admin_only_grant_permission_ids(db: Session) -> dict[UUID, str]:
    """Catalog ids (any state) of the admin-only-grant permissions."""
    rows = (
        db.query(Permission.id, Permission.key)
        .filter(Permission.key.in_(ADMIN_ONLY_GRANT_PERMISSION_KEYS))
        .all()
    )
    return {row[0]: row[1] for row in rows}


def direct_only_permission_ids(db: Session) -> dict[UUID, str]:
    rows = (
        db.query(Permission.id, Permission.key)
        .filter(Permission.key.in_(DIRECT_ONLY_PERMISSION_KEYS))
        .all()
    )
    return {row[0]: row[1] for row in rows}


def refused_admin_only_grant_keys(
    db: Session, *, actor: str, touched_permission_ids: Iterable[UUID]
) -> tuple[str, ...]:
    """Admin-only keys the actor may not grant or revoke here (empty = allowed)."""
    touched = set(touched_permission_ids)
    if not touched:
        return ()
    gated = admin_only_grant_permission_ids(db)
    keys = sorted(key for pid, key in gated.items() if pid in touched)
    if not keys or actor_holds_admin_role(db, actor):
        return ()
    return tuple(keys)


def _stage_refusal_audit(
    db: Session, *, context, operation_name: str, keys, refusal: str
) -> None:
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import AuditActor, stage_audit_event

    prefix, _, identifier = context.actor.partition(":")
    try:
        actor = AuditActor(
            actor_type=AuditActorType(prefix), actor_id=identifier or prefix
        )
    except ValueError:
        actor = AuditActor.system(context.actor)
    stage_audit_event(
        db,
        action="auth.rbac_admin_only_grant_refused",
        entity_type="permission",
        actor=actor,
        request_id=str(context.correlation_id),
        is_success=False,
        status_code=403,
        metadata={
            "schema_version": 1,
            "outcome": "refused",
            "refusal": refusal,
            "operation": operation_name,
            "permission_keys": list(keys),
            "reason": context.reason,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
    )


def forbidden_direct_only_role_keys(
    db: Session, role_addition_permission_ids: Iterable[UUID]
) -> tuple[str, ...]:
    """Direct-only keys among permissions about to be put on a role."""
    added = set(role_addition_permission_ids)
    if not added:
        return ()
    return tuple(
        sorted(
            key for pid, key in direct_only_permission_ids(db).items() if pid in added
        )
    )


def guard_admin_only_grants(
    db: Session,
    *,
    context,
    operation_name: str,
    touched: Callable[[], Iterable[UUID]],
    make_error: Callable[[tuple[str, ...]], Exception],
    operation: Callable[[], object],
    commit: Callable[[], None],
    role_additions: Callable[[], Iterable[UUID]] | None = None,
    make_direct_only_error: Callable[[tuple[str, ...]], Exception] | None = None,
):
    """Wrap an RBAC owner operation with the admin-only-grant rules.

    Two refusals, both audited in their own committed event (the owner
    transaction would roll an in-transaction audit back) and both repeated
    inside the owner transaction as a backstop:

    * a direct-only key may never be put on a role (``role_additions``), even
      for an admin;
    * an admin-only key may be granted or revoked only by an admin
      (``touched``).

    Names only; no secrets. ``commit`` is the caller's own session commit (this
    module owns no persistence).
    """

    def evaluate() -> tuple[str, tuple[str, ...]] | None:
        if role_additions is not None:
            keys = forbidden_direct_only_role_keys(db, role_additions())
            if keys:
                return "direct_only_permission", keys
        keys = refused_admin_only_grant_keys(
            db, actor=context.actor, touched_permission_ids=touched()
        )
        if keys:
            return "admin_role_required", keys
        return None

    def error_for(refusal: str, keys: tuple[str, ...]) -> Exception:
        if refusal == "direct_only_permission" and make_direct_only_error is not None:
            return make_direct_only_error(keys)
        return make_error(keys)

    def checked():
        refused = evaluate()
        if refused:
            raise error_for(*refused)
        return operation()

    if not db.in_transaction():
        refused = evaluate()
        if refused:
            _stage_refusal_audit(
                db,
                context=context,
                operation_name=operation_name,
                keys=refused[1],
                refusal=refused[0],
            )
            commit()
            raise error_for(*refused)
        if db.in_transaction():
            commit()
    return checked
