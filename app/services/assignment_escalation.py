"""No-escalation rule for every ``rbac:assign`` grant path (SUB-1 / G1).

Holding ``rbac:assign`` lets an actor *administer* assignments; it must not let
the actor hand out access the actor does not itself hold. The rule, enforced
inside each owner transaction by the path that mutates the grant:

* (a) subset: every permission a granted role carries, every directly granted
  permission and every API-key scope must be covered by the actor's own
  effective permission keys (wildcard-aware, ``permission_keys_allow``). The
  ``admin`` role is a grant of ``*``.
* (b) no self-grant: a non-admin actor may not add roles or permissions to its
  own account (a user to its own principal, an API key to its owning
  principal).

Break-glass: an actor holding the active ``admin`` role is the only existing
superuser path (``require_permission`` already short-circuits on it and
``actor_holds_admin_role`` already gates the admin-only grant keys). It is
exempt from both rules, unchanged. ``system`` and ``service`` actors are
internal, non-HTTP principals and are not evaluated here.

Names only in errors; no secrets.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import ApiKey
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberRole,
    SystemUserRole,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services.domain_errors import DomainError
from app.services.permission_authorization import (
    PermissionPrincipal,
    PermissionPrincipalKind,
    effective_permission_keys,
)
from app.services.permission_matching import permission_keys_allow

CODE_PREFIX = "auth.assignment_escalation"
EXCEEDS = f"{CODE_PREFIX}.exceeds_actor_permissions"
SELF = f"{CODE_PREFIX}.self_assignment"
UNKNOWN_ACTOR = f"{CODE_PREFIX}.unknown_actor"


class AssignmentEscalationError(DomainError):
    """The actor tried to grant access it does not hold, or to self-grant."""


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _unknown_actor() -> AssignmentEscalationError:
    return AssignmentEscalationError(
        code=UNKNOWN_ACTOR,
        message="The acting principal could not be resolved for this grant.",
        retryable=False,
    )


def _role_names(db: Session, principal_kind: PermissionPrincipalKind, pid: UUID):
    if principal_kind is PermissionPrincipalKind.system_user:
        stmt = (
            select(Role.name)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .where(SystemUserRole.system_user_id == pid)
        )
    else:
        stmt = (
            select(Role.name)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .where(SubscriberRole.subscriber_id == pid)
        )
    return frozenset(db.execute(stmt.where(Role.is_active.is_(True))).scalars().all())


class ActorGrantAuthority:
    """The actor's effective keys and break-glass status, resolved once."""

    def __init__(
        self,
        *,
        exempt: bool,
        admin: bool,
        keys: frozenset[str],
        own_principal_ids: frozenset[UUID],
    ) -> None:
        self.exempt = exempt  # internal system/service actor: not evaluated
        self.admin = admin  # active admin role: break-glass, unchanged
        self.keys = keys
        self.own_principal_ids = own_principal_ids

    def covers(self, key: str) -> bool:
        return self.exempt or self.admin or permission_keys_allow(self.keys, key)


def resolve_actor(
    db: Session, *, actor_type: AuditActorType, actor_id: str
) -> ActorGrantAuthority:
    if actor_type in (AuditActorType.system, AuditActorType.service):
        return ActorGrantAuthority(
            exempt=True, admin=False, keys=frozenset(), own_principal_ids=frozenset()
        )
    pid = _parse_uuid(actor_id)
    if pid is None:
        raise _unknown_actor()
    if actor_type is AuditActorType.api_key:
        key = db.get(ApiKey, pid)
        if key is None or not key.is_active or key.revoked_at is not None:
            raise _unknown_actor()
        owners = {
            owner
            for owner in (key.subscriber_id, getattr(key, "system_user_id", None))
            if owner is not None
        }
        return ActorGrantAuthority(
            exempt=False,
            admin=False,
            keys=frozenset(str(item) for item in (key.scopes or ())),
            own_principal_ids=frozenset(owners),
        )
    if actor_type is AuditActorType.user:
        if db.get(SystemUser, pid) is not None:
            kind = PermissionPrincipalKind.system_user
        elif db.get(Subscriber, pid) is not None:
            kind = PermissionPrincipalKind.subscriber
        else:
            raise _unknown_actor()
        roles = _role_names(db, kind, pid)
        principal = PermissionPrincipal(
            principal_id=pid,
            principal_type=kind,
            roles=roles,
            scopes=frozenset(),
        )
        return ActorGrantAuthority(
            exempt=False,
            admin="admin" in roles,
            keys=effective_permission_keys(principal, db),
            own_principal_ids=frozenset({pid}),
        )
    raise _unknown_actor()


def role_permission_keys(db: Session, role_ids: Iterable[UUID]) -> dict[str, set[str]]:
    """Map role name -> permission keys it confers (``admin`` confers ``*``)."""
    ids = set(role_ids)
    if not ids:
        return {}
    roles = db.execute(select(Role).where(Role.id.in_(ids))).scalars().all()
    result: dict[str, set[str]] = {}
    for role in roles:
        keys = set(
            db.execute(
                select(Permission.key)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(
                    RolePermission.role_id == role.id,
                    Permission.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        if role.name.lower() == "admin":
            keys.add("*")
        result[role.name] = keys
    return result


def permission_keys_for_ids(db: Session, permission_ids: Iterable[UUID]) -> set[str]:
    ids = set(permission_ids)
    if not ids:
        return set()
    return set(
        db.execute(select(Permission.key).where(Permission.id.in_(ids))).scalars().all()
    )


def _refuse_exceeds(missing: Iterable[str]) -> AssignmentEscalationError:
    return AssignmentEscalationError(
        code=EXCEEDS,
        message="You cannot grant access that you do not hold yourself.",
        details={"permission_keys": sorted(set(missing))},
        retryable=False,
    )


def enforce_key_subset(authority: ActorGrantAuthority, keys: Iterable[str]) -> None:
    """Refuse unless every requested key (or API-key scope) is covered."""
    missing = [key for key in keys if not authority.covers(key)]
    if missing:
        raise _refuse_exceeds(missing)


def enforce_grant(
    db: Session,
    *,
    actor_type: AuditActorType,
    actor_id: str,
    target_principal_ids: Iterable[UUID],
    role_ids: Iterable[UUID] = (),
    permission_ids: Iterable[UUID] = (),
    check_self: bool = True,
) -> None:
    """Apply rules (a) and (b) to roles/permissions about to be ADDED."""
    authority = resolve_actor(db, actor_type=actor_type, actor_id=actor_id)
    if authority.exempt or authority.admin:
        return
    if check_self and authority.own_principal_ids.intersection(target_principal_ids):
        raise AssignmentEscalationError(
            code=SELF,
            message="You cannot grant roles or permissions to your own account.",
            retryable=False,
        )
    needed: set[str] = set()
    for keys in role_permission_keys(db, role_ids).values():
        needed |= keys
    needed |= permission_keys_for_ids(db, permission_ids)
    enforce_key_subset(authority, needed)


def enforce_api_key_scopes(
    db: Session,
    *,
    actor_type: AuditActorType,
    actor_id: str,
    scopes: Iterable[str],
) -> None:
    """Refuse API-key scopes that exceed the actor's own effective keys."""
    requested = [str(scope).strip() for scope in scopes]
    if not requested:
        return  # nothing to exceed
    authority = resolve_actor(db, actor_type=actor_type, actor_id=actor_id)
    if authority.exempt or authority.admin:
        return
    enforce_key_subset(authority, requested)


ADMIN_REFUSED = f"{CODE_PREFIX}.admin_or_admin_only_role_refused"


def enforce_machine_role_grant(
    db: Session,
    *,
    actor_type: AuditActorType,
    actor_id: str,
    target_principal_ids: Iterable[UUID],
    role_ids: Iterable[UUID],
) -> None:
    """Named widening for machine callers (ERP HR sync key, service actors).

    They skip the subset rule, but may never grant the ``admin`` role, a role
    carrying an admin-only-grant key, or any role to the key's own owner.
    """
    from app.services.permission_authorization import ADMIN_ONLY_GRANT_PERMISSION_KEYS

    own: frozenset[UUID] = frozenset()
    if actor_type is AuditActorType.api_key:
        # Kernel machine credentials have no api_keys row: no owner to compare,
        # but the admin / admin-only refusal below still applies.
        try:
            own = resolve_actor(
                db, actor_type=actor_type, actor_id=actor_id
            ).own_principal_ids
        except AssignmentEscalationError:
            own = frozenset()
    roles = role_permission_keys(db, role_ids)
    refused = sorted(
        name
        for name, keys in roles.items()
        if "*" in keys or keys & ADMIN_ONLY_GRANT_PERMISSION_KEYS
    )
    if refused:
        raise AssignmentEscalationError(
            code=ADMIN_REFUSED,
            message="Machine callers may not grant the admin role or admin-only roles.",
            details={"roles": refused},
            retryable=False,
        )
    if roles and own.intersection(target_principal_ids):
        raise AssignmentEscalationError(
            code=SELF,
            message="You cannot grant roles or permissions to your own account.",
            retryable=False,
        )
