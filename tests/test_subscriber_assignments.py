"""Atomicity and policy tests for the subscriber assignment owner."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.rbac import Permission, Role, SubscriberPermission, SubscriberRole
from app.models.subscriber import Subscriber
from app.services import subscriber_assignments
from app.services.owner_commands import CommandContext, OwnerCommandError


def _context(
    *,
    scope: str = subscriber_assignments.ASSIGNMENT_SCOPE,
    key: str = "subscriber-assignment-test",
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:subscriber-assignment-test",
        scope=scope,
        reason="Verify canonical subscriber assignment semantics",
        idempotency_key=f"{key}:{command_id}",
    )


def _role(db_session, name: str = "support", *, active: bool = True) -> Role:
    role = Role(name=f"{name}-{uuid4().hex[:8]}", is_active=active)
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)
    db_session.expunge(role)
    db_session.commit()
    return role


def _permission(
    db_session,
    key: str = "ticket:read",
    *,
    active: bool = True,
    assignable: bool = True,
) -> Permission:
    permission = Permission(
        key=f"{key}_{uuid4().hex[:8]}",
        is_active=active,
        is_ui_assignable=assignable,
    )
    db_session.add(permission)
    db_session.commit()
    db_session.refresh(permission)
    db_session.expunge(permission)
    db_session.commit()
    return permission


def _subscriber(db_session, email_prefix: str) -> Subscriber:
    subscriber = Subscriber(
        first_name="Assignment",
        last_name="Target",
        email=f"{email_prefix}-{uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    db_session.expunge(subscriber)
    db_session.commit()
    return subscriber


def _committed_id(db_session, row: Any) -> UUID:
    row_id = row.id
    db_session.commit()
    return row_id


def test_role_grant_commits_assignment_audit_event_and_scope(db_session, subscriber):
    subscriber_id = _committed_id(db_session, subscriber)
    role = _role(db_session)

    result = subscriber_assignments.grant_subscriber_role(
        db_session,
        subscriber_assignments.GrantSubscriberRoleCommand(
            context=_context(),
            subscriber_id=subscriber_id,
            role_id=role.id,
            scope_type=" REGION ",
            scope_id=" Abuja-North ",
        ),
    )

    assert result.scope_type == "region"
    assert result.scope_id == "Abuja-North"
    assert result.changed is True
    assert not db_session.in_transaction()
    grant = db_session.get(SubscriberRole, result.id)
    assert grant is not None
    audit = db_session.query(AuditEvent).filter_by(entity_id=str(subscriber_id)).one()
    event = (
        db_session.query(EventStore)
        .filter_by(event_type="subscriber.assignments_changed")
        .one()
    )
    assert audit.action == "auth.subscriber_assignments.role_granted"
    assert audit.metadata_["role_grants"][0]["scope_type"] == "region"
    assert event.payload["aggregate_id"] == str(subscriber_id)
    assert event.payload["schema_version"] == 1


def test_duplicate_role_grant_is_idempotent(db_session, subscriber):
    subscriber_id = _committed_id(db_session, subscriber)
    role = _role(db_session)
    first = subscriber_assignments.grant_subscriber_role(
        db_session,
        subscriber_assignments.GrantSubscriberRoleCommand(
            context=_context(), subscriber_id=subscriber_id, role_id=role.id
        ),
    )
    second = subscriber_assignments.grant_subscriber_role(
        db_session,
        subscriber_assignments.GrantSubscriberRoleCommand(
            context=_context(), subscriber_id=subscriber_id, role_id=role.id
        ),
    )

    assert second.id == first.id
    assert second.changed is False
    assert db_session.query(SubscriberRole).count() == 1
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="subscriber.assignments_changed")
        .count()
        == 1
    )
    assert db_session.query(AuditEvent).count() == 2


@pytest.mark.parametrize(
    ("scope_type", "scope_id"),
    (("region", ""), ("", "orphan-id"), ("customer", "123")),
)
def test_invalid_scope_rolls_back_all_evidence(
    db_session, subscriber, scope_type, scope_id
):
    subscriber_id = _committed_id(db_session, subscriber)
    role = _role(db_session)

    with pytest.raises(subscriber_assignments.SubscriberAssignmentError) as captured:
        subscriber_assignments.grant_subscriber_role(
            db_session,
            subscriber_assignments.GrantSubscriberRoleCommand(
                context=_context(),
                subscriber_id=subscriber_id,
                role_id=role.id,
                scope_type=scope_type,
                scope_id=scope_id,
            ),
        )

    assert captured.value.code == "auth.subscriber_assignments.invalid_scope"
    assert not db_session.in_transaction()
    assert db_session.query(SubscriberRole).count() == 0
    assert db_session.query(AuditEvent).count() == 0
    assert db_session.query(EventStore).count() == 0


def test_inactive_catalog_and_nonassignable_permission_fail_closed(
    db_session, subscriber
):
    subscriber_id = _committed_id(db_session, subscriber)
    role = _role(db_session, active=False)
    permission = _permission(db_session, assignable=False)

    with pytest.raises(subscriber_assignments.SubscriberAssignmentError) as captured:
        subscriber_assignments.grant_subscriber_role(
            db_session,
            subscriber_assignments.GrantSubscriberRoleCommand(
                context=_context(), subscriber_id=subscriber_id, role_id=role.id
            ),
        )
    assert captured.value.code == "auth.subscriber_assignments.role_not_found"

    with pytest.raises(subscriber_assignments.SubscriberAssignmentError) as captured:
        subscriber_assignments.grant_subscriber_permission(
            db_session,
            subscriber_assignments.GrantSubscriberPermissionCommand(
                context=_context(),
                subscriber_id=subscriber_id,
                permission_id=permission.id,
            ),
        )
    assert captured.value.code == "auth.subscriber_assignments.permission_not_found"
    assert db_session.query(SubscriberRole).count() == 0
    assert db_session.query(SubscriberPermission).count() == 0


def test_replace_converges_roles_and_permissions(db_session, subscriber):
    subscriber_id = _committed_id(db_session, subscriber)
    old_role = _role(db_session, "old")
    scoped_role = _role(db_session, "scoped")
    old_permission = _permission(db_session, "ticket:old")
    desired_permission = _permission(db_session, "ticket:desired")
    db_session.add_all(
        (
            SubscriberRole(subscriber_id=subscriber_id, role_id=old_role.id),
            SubscriberPermission(
                subscriber_id=subscriber_id,
                permission_id=old_permission.id,
            ),
        )
    )
    db_session.commit()

    result = subscriber_assignments.replace_subscriber_assignments(
        db_session,
        subscriber_assignments.ReplaceSubscriberAssignmentsCommand(
            context=_context(),
            subscriber_id=subscriber_id,
            role_grants=(
                subscriber_assignments.SubscriberRoleGrantSpec(
                    role_id=scoped_role.id,
                    scope_type="reseller",
                    scope_id="reseller-42",
                ),
            ),
            direct_permission_ids=(desired_permission.id,),
        ),
    )

    assert result.changed is True
    assert result.role_grants == (
        subscriber_assignments.SubscriberRoleGrantSpec(
            role_id=scoped_role.id,
            scope_type="reseller",
            scope_id="reseller-42",
        ),
    )
    assert result.direct_permission_keys == (desired_permission.key,)
    assert db_session.query(SubscriberRole).one().role_id == scoped_role.id
    assert (
        db_session.query(SubscriberPermission).one().permission_id
        == desired_permission.id
    )


def test_direct_permission_commands_grant_move_update_and_revoke(
    db_session, subscriber
):
    subscriber_id = _committed_id(db_session, subscriber)
    target = _subscriber(db_session, "permission-move")
    first_permission = _permission(db_session, "ticket:first")
    second_permission = _permission(db_session, "ticket:second")

    granted = subscriber_assignments.grant_subscriber_permission(
        db_session,
        subscriber_assignments.GrantSubscriberPermissionCommand(
            context=_context(),
            subscriber_id=subscriber_id,
            permission_id=first_permission.id,
        ),
    )
    updated = subscriber_assignments.update_subscriber_permission(
        db_session,
        subscriber_assignments.UpdateSubscriberPermissionCommand(
            context=_context(),
            grant_id=granted.id,
            subscriber_id=target.id,
            permission_id=second_permission.id,
            granted_by_subscriber_id=subscriber_id,
            update_granted_by=True,
        ),
    )

    assert updated.subscriber_id == target.id
    assert updated.permission_id == second_permission.id
    assert updated.granted_by_subscriber_id == subscriber_id
    subscriber_assignments.revoke_subscriber_permission(
        db_session,
        subscriber_assignments.RevokeSubscriberPermissionCommand(
            context=_context(), grant_id=granted.id
        ),
    )
    assert db_session.query(SubscriberPermission).count() == 0
    assert db_session.query(AuditEvent).count() == 3
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="subscriber.assignments_changed")
        .count()
        == 3
    )


def test_update_moving_grant_invalidates_both_subscribers_after_commit(
    db_session, subscriber, monkeypatch
):
    subscriber_id = _committed_id(db_session, subscriber)
    target = _subscriber(db_session, "assignment-move")
    role = _role(db_session)
    grant = SubscriberRole(subscriber_id=subscriber_id, role_id=role.id)
    db_session.add(grant)
    db_session.flush()
    grant_id = grant.id
    db_session.commit()
    invalidated: list[tuple[str, str]] = []
    monkeypatch.setattr(
        subscriber_assignments.auth_cache,
        "invalidate_principal",
        lambda principal_type, principal_id: invalidated.append(
            (principal_type, principal_id)
        ),
    )

    result = subscriber_assignments.update_subscriber_role(
        db_session,
        subscriber_assignments.UpdateSubscriberRoleCommand(
            context=_context(),
            grant_id=grant_id,
            subscriber_id=target.id,
        ),
    )

    assert result.subscriber_id == target.id
    assert set(invalidated) == {
        ("subscriber", str(subscriber_id)),
        ("subscriber", str(target.id)),
    }


def test_late_audit_failure_rolls_back_assignment(db_session, subscriber, monkeypatch):
    subscriber_id = _committed_id(db_session, subscriber)
    role = _role(db_session)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(subscriber_assignments, "stage_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        subscriber_assignments.grant_subscriber_role(
            db_session,
            subscriber_assignments.GrantSubscriberRoleCommand(
                context=_context(), subscriber_id=subscriber_id, role_id=role.id
            ),
        )

    assert not db_session.in_transaction()
    assert db_session.query(SubscriberRole).count() == 0
    assert db_session.query(EventStore).count() == 0


def test_public_command_rejects_active_caller_transaction(db_session, subscriber):
    subscriber_id = _committed_id(db_session, subscriber)
    role = _role(db_session)
    db_session.get(Subscriber, subscriber_id)

    with pytest.raises(OwnerCommandError) as captured:
        subscriber_assignments.grant_subscriber_role(
            db_session,
            subscriber_assignments.GrantSubscriberRoleCommand(
                context=_context(), subscriber_id=subscriber_id, role_id=role.id
            ),
        )

    assert captured.value.code.endswith(".active_caller_transaction")
    db_session.rollback()


def test_coordinator_collaborator_flushes_without_committing(db_session, subscriber):
    subscriber_id = _committed_id(db_session, subscriber)
    role = _role(db_session)

    grant = subscriber_assignments.ensure_role_grant_in_transaction(
        db_session,
        context=_context(),
        subscriber_id=subscriber_id,
        role_id=role.id,
    )

    assert grant.id is not None
    assert db_session.in_transaction()
    assert db_session.query(AuditEvent).count() == 1
    assert db_session.query(EventStore).count() == 1
    db_session.rollback()
    assert db_session.query(SubscriberRole).count() == 0
