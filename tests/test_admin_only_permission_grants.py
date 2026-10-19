"""billing:starter_ledger_live:write may be granted or revoked only by an admin."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.system_user import SystemUser
from app.services import rbac_catalog, system_user_assignments
from app.services.owner_commands import CommandContext

KEY = "billing:starter_ledger_live:write"


def _user(db) -> SystemUser:
    user = SystemUser(
        id=uuid4(), first_name="A", last_name="B", email=f"a-{uuid4().hex}@x.test"
    )
    db.add(user)
    db.commit()
    return user


def _admin(db) -> SystemUser:
    role = db.scalars(select(Role).where(Role.name == "admin")).first()
    if role is None:
        role = Role(name="admin", is_active=True)
        db.add(role)
        db.flush()
    user = _user(db)
    db.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db.commit()
    return user


def _permission(db) -> Permission:
    permission = db.scalars(select(Permission).where(Permission.key == KEY)).first()
    if permission is None:
        permission = Permission(key=KEY, description="test")
        db.add(permission)
        db.commit()
    return permission


def _role(db) -> Role:
    role = Role(name=f"r-{uuid4().hex[:8]}", is_active=True)
    db.add(role)
    db.commit()
    return role


def _ctx(user: SystemUser, scope: str) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{user.id}", scope=scope, reason="grant test"
    )


def _refusals(db) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.rbac_admin_only_grant_refused"
            )
        )
    )


def _assign(db, actor: SystemUser, target: SystemUser, permission_ids):
    role_ids = tuple(
        db.scalars(
            select(SystemUserRole.role_id).where(
                SystemUserRole.system_user_id == target.id
            )
        )
    )
    db.commit()  # owner commands need an idle session
    return system_user_assignments.replace_system_user_assignments(
        db,
        system_user_assignments.ReplaceSystemUserAssignmentsCommand(
            context=_ctx(actor, system_user_assignments.ASSIGNMENT_SCOPE),
            user_id=target.id,
            role_ids=role_ids,
            direct_permission_ids=tuple(permission_ids),
        ),
    )


def test_non_admin_direct_grant_is_refused_and_audited(db_session) -> None:
    permission = _permission(db_session)
    actor, target = _user(db_session), _user(db_session)
    with pytest.raises(system_user_assignments.SystemUserAssignmentError) as caught:
        _assign(db_session, actor, target, [permission.id])
    assert caught.value.code.endswith("admin_role_required")
    assert (
        db_session.scalars(
            select(SystemUserPermission).where(
                SystemUserPermission.system_user_id == target.id
            )
        ).first()
        is None
    )
    refusals = _refusals(db_session)
    assert len(refusals) == 1
    assert refusals[0].is_success is False
    assert refusals[0].metadata_["permission_keys"] == [KEY]


def test_admin_direct_grant_succeeds_including_to_self(db_session) -> None:
    permission = _permission(db_session)
    admin = _admin(db_session)
    other = _user(db_session)
    _assign(db_session, admin, other, [permission.id])
    # An admin granting it to themselves is allowed.
    _assign(db_session, admin, admin, [permission.id])
    held = {
        row.system_user_id
        for row in db_session.scalars(select(SystemUserPermission))
        if row.permission_id == permission.id
    }
    assert {other.id, admin.id} <= held
    assert _refusals(db_session) == []
    assert any(
        e.action == "auth.system_user_assignments_replaced"
        for e in db_session.scalars(select(AuditEvent))
    )


def test_non_admin_cannot_revoke_a_direct_grant(db_session) -> None:
    permission = _permission(db_session)
    admin, actor, target = _admin(db_session), _user(db_session), _user(db_session)
    _assign(db_session, admin, target, [permission.id])
    with pytest.raises(system_user_assignments.SystemUserAssignmentError):
        _assign(db_session, actor, target, [])
    assert len(_refusals(db_session)) == 1


def test_non_admin_adding_it_to_a_role_is_refused_and_audited(db_session) -> None:
    permission = _permission(db_session)
    actor, role = _user(db_session), _role(db_session)
    with pytest.raises(rbac_catalog.RbacCatalogError) as caught:
        db_session.commit()  # owner commands need an idle session
        rbac_catalog.grant_role_permission(
            db_session,
            rbac_catalog.GrantRolePermissionCommand(
                context=_ctx(actor, rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=role.id,
                permission_id=permission.id,
            ),
        )
    assert caught.value.code.endswith("admin_role_required")
    assert db_session.scalars(select(RolePermission)).first() is None
    assert len(_refusals(db_session)) == 1


def test_non_admin_role_update_with_the_permission_is_refused(db_session) -> None:
    permission = _permission(db_session)
    actor, role = _user(db_session), _role(db_session)
    with pytest.raises(rbac_catalog.RbacCatalogError):
        db_session.commit()  # owner commands need an idle session
        rbac_catalog.update_role(
            db_session,
            rbac_catalog.UpdateRoleCommand(
                context=_ctx(actor, rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=role.id,
                permission_ids=(permission.id,),
            ),
        )
    assert len(_refusals(db_session)) == 1


def test_admin_adding_it_to_a_role_succeeds(db_session) -> None:
    permission = _permission(db_session)
    admin, role = _admin(db_session), _role(db_session)
    db_session.commit()  # owner commands need an idle session
    rbac_catalog.grant_role_permission(
        db_session,
        rbac_catalog.GrantRolePermissionCommand(
            context=_ctx(admin, rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=role.id,
            permission_id=permission.id,
        ),
    )
    assert (
        db_session.scalars(
            select(RolePermission).where(RolePermission.role_id == role.id)
        ).first()
        is not None
    )
    assert _refusals(db_session) == []


def test_non_admin_cannot_revoke_a_role_grant(db_session) -> None:
    permission = _permission(db_session)
    admin, actor, role = _admin(db_session), _user(db_session), _role(db_session)
    db_session.commit()  # owner commands need an idle session
    outcome = rbac_catalog.grant_role_permission(
        db_session,
        rbac_catalog.GrantRolePermissionCommand(
            context=_ctx(admin, rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=role.id,
            permission_id=permission.id,
        ),
    )
    with pytest.raises(rbac_catalog.RbacCatalogError):
        db_session.commit()  # owner commands need an idle session
        rbac_catalog.revoke_role_permission(
            db_session,
            rbac_catalog.RevokeRolePermissionCommand(
                context=_ctx(actor, rbac_catalog.ROLE_WRITE_SCOPE),
                link_id=outcome.id,
            ),
        )
    assert len(_refusals(db_session)) == 1


def test_other_permissions_are_unaffected_for_non_admins(db_session) -> None:
    other = Permission(key=f"x:{uuid4().hex[:6]}:read", description="t")
    db_session.add(other)
    db_session.commit()
    actor, target = _user(db_session), _user(db_session)
    _assign(db_session, actor, target, [other.id])
    assert _refusals(db_session) == []


def test_seed_does_not_grant_it_to_the_admin_role() -> None:
    from scripts.seed import seed_rbac

    assert KEY in {key for key, _ in seed_rbac.DEFAULT_PERMISSIONS}
    assert KEY not in seed_rbac.ROLE_PERMISSIONS["admin"]
