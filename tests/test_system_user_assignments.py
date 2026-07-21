"""Atomicity, source ownership, and safety tests for staff access grants."""

from __future__ import annotations

import uuid

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.rbac import Permission, Role, SystemUserPermission, SystemUserRole
from app.models.system_user import SystemUser
from app.services import staff_provisioning, system_user_assignments
from app.services.owner_commands import CommandContext


def _context(key: str = "assignment-owner-test") -> CommandContext:
    command_id = uuid.uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="service:assignment-test",
        scope=system_user_assignments.ASSIGNMENT_SCOPE,
        reason="verify canonical system-user assignment semantics",
        idempotency_key=key,
    )


def _user(email: str) -> SystemUser:
    return SystemUser(
        first_name="Assignment",
        last_name="Test",
        email=email,
        is_active=True,
    )


def _replace(
    db_session,
    *,
    user_id,
    role_ids=(),
    permission_ids=(),
):
    return system_user_assignments.replace_system_user_assignments(
        db_session,
        system_user_assignments.ReplaceSystemUserAssignmentsCommand(
            context=_context(),
            user_id=user_id,
            role_ids=tuple(role_ids),
            direct_permission_ids=tuple(permission_ids),
        ),
    )


def test_replace_commits_grants_audit_event_and_preserves_managed_roles(
    db_session,
) -> None:
    managed_role = Role(name="erp-managed", description="Managed")
    local_role = Role(name="local-operator", description="Local")
    permission = Permission(key="subscriber:read", description="Read")
    user = _user("assignment-owner@dotmac.io")
    db_session.add_all((managed_role, local_role, permission, user))
    db_session.flush()
    managed_role_id = managed_role.id
    local_role_id = local_role.id
    permission_id = permission.id
    user_id = user.id
    db_session.add(
        SystemUserRole(
            system_user_id=user_id,
            role_id=managed_role_id,
            source="erp_hr",
        )
    )
    db_session.commit()

    result = _replace(
        db_session,
        user_id=user_id,
        role_ids=(local_role_id,),
        permission_ids=(permission_id,),
    )

    assert result.changed is True
    assert result.role_names == ("erp-managed", "local-operator")
    assert result.direct_permission_keys == ("subscriber:read",)
    grants = db_session.query(SystemUserRole).filter_by(system_user_id=user_id).all()
    assert {(grant.role_id, grant.source) for grant in grants} == {
        (managed_role_id, "erp_hr"),
        (local_role_id, "local"),
    }
    assert (
        db_session.query(SystemUserPermission).filter_by(system_user_id=user_id).count()
        == 1
    )
    assert (
        db_session.query(AuditEvent).filter_by(entity_id=str(user_id)).one().action
        == "auth.system_user_assignments_replaced"
    )
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="system_user.assignments_changed")
        .count()
        == 1
    )


def test_invalid_permission_rolls_back_role_replacement(db_session) -> None:
    old_role = Role(name="old-local", description="Old")
    new_role = Role(name="new-local", description="New")
    blocked = Permission(
        key="internal:dangerous",
        description="Not UI assignable",
        is_ui_assignable=False,
    )
    user = _user("assignment-rollback@dotmac.io")
    db_session.add_all((old_role, new_role, blocked, user))
    db_session.flush()
    old_role_id = old_role.id
    new_role_id = new_role.id
    blocked_id = blocked.id
    user_id = user.id
    db_session.add(SystemUserRole(system_user_id=user_id, role_id=old_role_id))
    db_session.commit()

    with pytest.raises(system_user_assignments.SystemUserAssignmentError) as captured:
        _replace(
            db_session,
            user_id=user_id,
            role_ids=(new_role_id,),
            permission_ids=(blocked_id,),
        )

    assert captured.value.code == "auth.system_user_assignments.invalid_permissions"
    assert not db_session.in_transaction()
    grants = db_session.query(SystemUserRole).filter_by(system_user_id=user_id).all()
    assert [(grant.role_id, grant.source) for grant in grants] == [
        (old_role_id, "local")
    ]
    assert db_session.query(AuditEvent).filter_by(entity_id=str(user_id)).count() == 0


def test_final_active_admin_cannot_lose_admin_role(db_session) -> None:
    admin = Role(name="admin", description="Full access")
    user = _user("final-admin@dotmac.io")
    db_session.add_all((admin, user))
    db_session.flush()
    user_id = user.id
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=admin.id))
    db_session.commit()

    with pytest.raises(system_user_assignments.SystemUserAssignmentError) as captured:
        _replace(db_session, user_id=user_id)

    assert captured.value.code == "auth.system_user_assignments.last_admin_required"
    assert not db_session.in_transaction()
    assert (
        db_session.query(SystemUserRole).filter_by(system_user_id=user_id).count() == 1
    )


def test_admin_role_can_be_removed_when_another_active_admin_remains(
    db_session,
) -> None:
    admin = Role(name="admin", description="Full access")
    first = _user("first-admin@dotmac.io")
    second = _user("second-admin@dotmac.io")
    db_session.add_all((admin, first, second))
    db_session.flush()
    first_id = first.id
    db_session.add_all(
        (
            SystemUserRole(system_user_id=first.id, role_id=admin.id),
            SystemUserRole(system_user_id=second.id, role_id=admin.id),
        )
    )
    db_session.commit()

    result = _replace(db_session, user_id=first_id)

    assert result.role_names == ()
    assert (
        db_session.query(SystemUserRole).filter_by(system_user_id=first_id).count() == 0
    )


def test_staff_owner_cannot_deactivate_final_active_admin(db_session) -> None:
    admin = Role(name="admin", description="Full access")
    user = _user("active-final-admin@dotmac.io")
    db_session.add_all((admin, user))
    db_session.flush()
    user_id = user.id
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=admin.id))
    db_session.commit()

    with pytest.raises(system_user_assignments.SystemUserAssignmentError) as captured:
        staff_provisioning.set_staff_account_active(
            db_session,
            staff_provisioning.SetStaffAccountActiveCommand(
                context=_context("deactivate-final-admin"),
                user_id=user_id,
                is_active=False,
            ),
        )

    assert captured.value.code == "auth.system_user_assignments.last_admin_required"
    assert not db_session.in_transaction()
    assert db_session.get(SystemUser, user_id).is_active is True


def test_late_audit_failure_rolls_back_all_assignment_changes(
    db_session, monkeypatch
) -> None:
    role = Role(name="rollback-role", description="Rollback")
    user = _user("late-assignment-failure@dotmac.io")
    db_session.add_all((role, user))
    db_session.flush()
    role_id = role.id
    user_id = user.id
    db_session.commit()

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(system_user_assignments, "stage_audit_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _replace(db_session, user_id=user_id, role_ids=(role_id,))

    assert not db_session.in_transaction()
    assert (
        db_session.query(SystemUserRole).filter_by(system_user_id=user_id).count() == 0
    )
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="system_user.assignments_changed")
        .count()
        == 0
    )
