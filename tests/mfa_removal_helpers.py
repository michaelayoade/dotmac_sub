"""Shared fixtures for MFA removal owner-command tests."""

from __future__ import annotations

from uuid import UUID, uuid4

from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.system_user import SystemUser
from app.services import staff_provisioning
from app.services.owner_commands import CommandContext


def make_context(
    actor_id: str | UUID,
    *,
    scope: str = staff_provisioning.STAFF_ASSIGN_SCOPE,
    actor_type: str = "user",
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{actor_id}",
        scope=scope,
        reason="test",
        idempotency_key=f"k-{command_id}",
    )


def make_staff(
    db, *, role: str | None = None, permissions: tuple[str, ...] = ()
) -> UUID:
    """Create a system user with an optional role carrying given permissions."""

    user = SystemUser(
        first_name="S", last_name="U", email=f"{uuid4().hex[:8]}@example.test"
    )
    db.add(user)
    db.flush()
    if role:
        row = db.query(Role).filter(Role.name == role).one_or_none()
        if row is None:
            row = Role(name=role, description=role)
            db.add(row)
            db.flush()
        for key in permissions:
            perm = db.query(Permission).filter(Permission.key == key).one_or_none()
            if perm is None:
                perm = Permission(key=key)
                db.add(perm)
                db.flush()
            if (
                db.query(RolePermission)
                .filter(
                    RolePermission.role_id == row.id,
                    RolePermission.permission_id == perm.id,
                )
                .first()
                is None
            ):
                db.add(RolePermission(role_id=row.id, permission_id=perm.id))
        db.add(SystemUserRole(system_user_id=user.id, role_id=row.id))
    uid = user.id
    db.commit()
    return uid


def make_admin(db) -> UUID:
    return make_staff(db, role="admin")
