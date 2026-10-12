"""Transport-neutral canonical permission calculation owned by auth.permission_gate.

Legacy HTTP wrappers and strict staff admission delegate to this calculator.
"""

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
