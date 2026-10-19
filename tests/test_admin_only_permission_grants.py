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


def _detach(db, obj):
    """Persist ``obj`` and return it detached with its attributes loaded.

    Owner commands need an idle session; reading an expired attribute after a
    commit would autobegin a transaction while building the command.
    """
    db.flush()
    db.expunge(obj)
    db.commit()
    return obj


def _user(db) -> SystemUser:
    user = SystemUser(
        id=uuid4(), first_name="A", last_name="B", email=f"a-{uuid4().hex}@x.test"
    )
    db.add(user)
    return _detach(db, user)


def _admin(db) -> SystemUser:
    role = db.scalars(select(Role).where(Role.name == "admin")).first()
    if role is None:
        role = Role(name="admin", is_active=True)
        db.add(role)
        db.flush()
    role_id = role.id
    user = _user(db)
    db.add(SystemUserRole(system_user_id=user.id, role_id=role_id))
    db.commit()
    return user


def _permission(db) -> Permission:
    permission = db.scalars(select(Permission).where(Permission.key == KEY)).first()
    if permission is None:
        permission = Permission(key=KEY, description="test")
        db.add(permission)
    return _detach(db, permission)


def _role(db) -> Role:
    role = Role(name=f"r-{uuid4().hex[:8]}", is_active=True)
    db.add(role)
    return _detach(db, role)


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
    assert caught.value.code.endswith("direct_only_permission")
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


def test_non_admin_cannot_revoke_a_legacy_role_grant(db_session) -> None:
    role = _legacy_role_with_key(db_session)
    actor = _user(db_session)
    link_id = db_session.scalars(
        select(RolePermission.id).where(RolePermission.role_id == role.id)
    ).one()
    db_session.commit()
    with pytest.raises(rbac_catalog.RbacCatalogError) as caught:
        rbac_catalog.revoke_role_permission(
            db_session,
            rbac_catalog.RevokeRolePermissionCommand(
                context=_ctx(actor, rbac_catalog.ROLE_WRITE_SCOPE),
                link_id=link_id,
            ),
        )
    assert caught.value.code.endswith("admin_role_required")
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


# ---- direct-only: role membership can never confer the key ------------------


def _legacy_role_with_key(db, *, active: bool = True) -> Role:
    """A role that somehow carries the key (raw rows, bypassing the services)."""
    permission = _permission(db)
    role = Role(name=f"legacy-{uuid4().hex[:8]}", is_active=active)
    db.add(role)
    db.flush()
    db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    return _detach(db, role)


def _holds(db, user: SystemUser) -> bool:
    from app.services.permission_authorization import (
        system_user_holds_exact_permission,
    )

    db.commit()
    return system_user_holds_exact_permission(db, user.id, KEY)


def _switch_on_refused(db, user: SystemUser) -> bool:
    from app.services import web_system_settings_forms
    from app.services.domain_settings import ADMIN_SETTINGS_FORM_WRITE_SCOPE

    db.commit()
    errors = web_system_settings_forms.set_starter_ledger_live_switch(
        db=db,
        enabled=True,
        context=CommandContext.system(
            actor=f"user:{user.id}",
            scope=ADMIN_SETTINGS_FORM_WRITE_SCOPE,
            reason="flip",
        ),
    )
    return bool(errors)


def test_role_carrying_the_key_does_not_satisfy_the_check(db_session) -> None:
    role = _legacy_role_with_key(db_session)
    user = _user(db_session)
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()
    assert _holds(db_session, user) is False
    assert _switch_on_refused(db_session, user) is True


def test_direct_grant_still_satisfies_the_check(db_session) -> None:
    permission = _permission(db_session)
    user = _user(db_session)
    db_session.add(
        SystemUserPermission(system_user_id=user.id, permission_id=permission.id)
    )
    db_session.commit()
    assert _holds(db_session, user) is True


def test_staff_provisioning_with_such_a_role_cannot_enable_the_switch(
    db_session,
) -> None:
    role = _legacy_role_with_key(db_session)
    by_ids, by_names = _user(db_session), _user(db_session)
    system_user_assignments.sync_source_roles_by_ids(
        db_session, user_id=by_ids.id, role_ids=(role.id,), source="local"
    )
    system_user_assignments.sync_source_roles_by_names(
        db_session, user_id=by_names.id, role_names=(role.name,), source="erp_hr"
    )
    db_session.commit()
    for user in (by_ids, by_names):
        assert _holds(db_session, user) is False
        assert _switch_on_refused(db_session, user) is True


def test_admin_cannot_put_the_key_on_a_role_and_it_is_audited(db_session) -> None:
    permission = _permission(db_session)
    admin, role = _admin(db_session), _role(db_session)
    db_session.commit()
    with pytest.raises(rbac_catalog.RbacCatalogError) as caught:
        rbac_catalog.grant_role_permission(
            db_session,
            rbac_catalog.GrantRolePermissionCommand(
                context=_ctx(admin, rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=role.id,
                permission_id=permission.id,
            ),
        )
    assert caught.value.code.endswith("direct_only_permission")
    db_session.commit()
    with pytest.raises(rbac_catalog.RbacCatalogError):
        rbac_catalog.update_role(
            db_session,
            rbac_catalog.UpdateRoleCommand(
                context=_ctx(admin, rbac_catalog.ROLE_WRITE_SCOPE),
                role_id=role.id,
                permission_ids=(permission.id,),
            ),
        )
    db_session.commit()
    with pytest.raises(rbac_catalog.RbacCatalogError):
        rbac_catalog.create_role(
            db_session,
            rbac_catalog.CreateRoleCommand(
                context=_ctx(admin, rbac_catalog.ROLE_WRITE_SCOPE),
                name=f"new-{uuid4().hex[:6]}",
                permission_ids=(permission.id,),
            ),
        )
    assert (
        db_session.scalars(
            select(RolePermission).where(RolePermission.role_id == role.id)
        ).first()
        is None
    )
    assert len(_refusals(db_session)) == 3


def test_reactivating_a_role_that_carries_the_key_grants_nothing(db_session) -> None:
    role = _legacy_role_with_key(db_session, active=False)
    admin, member = _admin(db_session), _user(db_session)
    db_session.add(SystemUserRole(system_user_id=member.id, role_id=role.id))
    db_session.commit()
    rbac_catalog.update_role(
        db_session,
        rbac_catalog.UpdateRoleCommand(
            context=_ctx(admin, rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=role.id,
            is_active=True,
        ),
    )
    assert _holds(db_session, member) is False
    assert _switch_on_refused(db_session, member) is True
