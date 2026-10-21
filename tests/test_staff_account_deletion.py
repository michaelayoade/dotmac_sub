"""Fast SQLite evidence only; hosted migrated PostgreSQL proves FK/locking."""

from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import set_committed_value

from app.models.audit import AuditEvent
from app.models.auth import ApiKey, MFAMethod, MFARecoveryCode, UserCredential
from app.models.system_user import SystemUser
from app.services import api_key_administration as keys
from app.services import staff_provisioning as owner
from app.services import system_user_assignments as grants
from app.services.audit_adapter import AuditActor
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import OwnerCommandError
from app.web.admin import system
from tests.staff_deletion_fixtures import (
    bind_party_fixture,
    bulk_command,
    command,
    staff_fixture,
)
from tests.support.tx_trace import assert_protected_tx, trace_transactions
from tests.test_web_api_key_administration import request_fixture


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("api_actor", [False, True])
def test_delete_and_exact_key_audits_share_root(db_session, bulk, api_actor):
    user_id, key_id = staff_fixture(db_session)
    cmd = bulk_command((user_id,)) if bulk else command(user_id)
    cmd = replace(
        cmd, context=replace(cmd.context, reason="excluded-untrusted-context-reason")
    )
    if api_actor:
        actor = AuditActor.api_key(str(uuid4()), label="excluded-actor-label")
        cmd = replace(
            cmd,
            actor=actor,
            context=replace(cmd.context, actor=f"api_key:{actor.actor_id}"),
        )
    with trace_transactions(db_session.get_bind().engine) as trace:
        result = (
            owner.bulk_delete_staff_accounts if bulk else owner.delete_staff_account
        )(db_session, command=cmd)
    assert_protected_tx(
        trace.result,
        protected_tables=frozenset(
            {"system_users", "api_keys", "user_credentials", "mfa_methods"}
        ),
    )
    assert result.deleted_user_ids == (user_id,) and result.deleted_key_ids == (key_id,)
    assert (
        db_session.get(SystemUser, user_id) is None
        and db_session.get(ApiKey, key_id) is None
    )
    audits = db_session.scalars(select(AuditEvent)).all()
    assert {row.action for row in audits} == {
        "auth.api_key.deleted",
        "auth.staff_account.deleted",
    }
    assert {row.entity_id for row in audits} == {str(user_id), str(key_id)}
    for row in audits:
        assert (
            row.actor_id == cmd.actor.actor_id
            and row.actor_type == cmd.actor.actor_type
        )
        assert row.details == {
            "schema_version": 1,
            "scope": "rbac:assign",
            "reason": "inactive_staff_account_deletion",
            "command_id": str(cmd.context.command_id),
            "correlation_id": str(cmd.context.correlation_id),
        }
        assert "excluded-untrusted-context-reason" not in repr(row.details)
        assert not any(
            secret in repr(row.details) + repr(cmd)
            for secret in (
                "inert-password-hash",
                "excluded-key-label",
                "excluded:scope",
                "excluded-actor-label",
                "excluded-untrusted-context-reason",
                "inert-seed",
                "@test.invalid",
            )
        )


@pytest.mark.parametrize(
    "failure", ["key_audit", "parent_audit", "commit", "late_bulk"]
)
def test_failure_rolls_back_every_deletion_and_audit(db_session, monkeypatch, failure):
    first, first_key = staff_fixture(db_session)
    second, second_key = staff_fixture(db_session)
    cmd = bulk_command((first, second))
    stage = owner.stage_audit_event

    def fail(*args, **kwargs):
        stage(*args, **kwargs)
        raise RuntimeError("inert audit failure")

    def fail_commit(db):
        raise RuntimeError("inert commit failure")

    def late_grant(db, *, command):
        if command.user_id == max((first, second), key=str):
            raise RuntimeError("inert late bulk failure")
        return grants.RemovedSystemUserAssignments(command.user_id)

    if failure == "commit":
        event.listen(db_session, "before_commit", fail_commit)
    elif failure == "late_bulk":
        monkeypatch.setattr(grants, "remove_all_for_system_user", late_grant)
    else:
        monkeypatch.setattr(
            keys if failure == "key_audit" else owner, "stage_audit_event", fail
        )
    try:
        with trace_transactions(db_session.get_bind().engine) as trace:
            with pytest.raises(RuntimeError):
                owner.bulk_delete_staff_accounts(db_session, command=cmd)
        assert_protected_tx(
            trace.result,
            protected_tables=frozenset({"api_keys", "user_credentials", "mfa_methods"}),
            success=False,
        )
        assert all(
            db_session.get(SystemUser, target) is not None for target in (first, second)
        )
        assert all(
            db_session.get(ApiKey, target) is not None
            for target in (first_key, second_key)
        )
        assert len(db_session.scalars(select(UserCredential)).all()) == 2
        assert len(db_session.scalars(select(MFAMethod)).all()) == 2
        assert not db_session.scalars(select(AuditEvent)).all()
    finally:
        if failure == "commit":
            event.remove(db_session, "before_commit", fail_commit)


def test_bulk_missing_active_and_duplicates_keep_counts(db_session):
    inactive, _ = staff_fixture(db_session)
    active, _ = staff_fixture(db_session, active=True)
    result = owner.bulk_delete_staff_accounts(
        db_session, command=bulk_command((inactive, active, uuid4(), inactive))
    )
    assert result.deleted_user_ids == (inactive,) and result.skipped_count == 3
    assert db_session.get(SystemUser, active) is not None
    assert len(db_session.scalars(select(AuditEvent)).all()) == 2


@pytest.mark.parametrize("case", ["missing", "active", "stale"])
def test_single_refuses_and_refreshes_standing(db_session, case):
    user_id = uuid4()
    if case != "missing":
        user_id, _ = staff_fixture(db_session, active=True)
        user = db_session.get(SystemUser, user_id)
        db_session.commit()
        if case == "stale":
            set_committed_value(user, "is_active", False)
    with pytest.raises(owner.StaffProvisioningError) as error:
        owner.delete_staff_account(db_session, command=command(user_id))
    assert error.value.code.endswith(
        ".staff_account_not_found" if case == "missing" else ".active_staff_deletion"
    )
    assert not db_session.scalars(select(AuditEvent)).all()


@pytest.mark.parametrize("case", ["actor", "scope", "target", "system_actor"])
def test_invalid_typed_context_denies_without_transaction(db_session, case):
    cmd = command(uuid4())
    if case in {"actor", "scope"}:
        cmd = replace(cmd, context=replace(cmd.context, **{case: "wrong"}))
    elif case == "target":
        cmd = replace(cmd, user_id="invalid")
    else:
        cmd = replace(cmd, actor=AuditActor.system())
    with pytest.raises(owner.StaffProvisioningError):
        owner.delete_staff_account(db_session, command=cmd)
    assert not db_session.in_transaction()


def test_participants_refuse_without_exact_active_parent(db_session):
    cmd = command(uuid4())
    with pytest.raises(DomainError, match="Trusted"):
        keys.stage_staff_key_deletion(
            db_session,
            command=keys.StaffKeyDeletionCommand(
                cmd.context, cmd.actor, (cmd.user_id,)
            ),
        )
    with pytest.raises(DomainError, match="Trusted"):
        grants.remove_all_for_system_user(
            db_session,
            command=grants.RemoveSystemUserAssignmentsCommand(cmd.context, cmd.user_id),
        )
    assert not db_session.in_transaction()


def test_active_request_transaction_is_not_consumed(db_session):
    db_session.execute(select(SystemUser))
    with pytest.raises(OwnerCommandError, match="transaction"):
        owner.delete_staff_account(db_session, command=command(uuid4()))


def test_sqlalchemy_storage_failure_is_sanitized(db_session, monkeypatch):
    user_id, key_id = staff_fixture(db_session)

    def fail(*args, **kwargs):
        raise OperationalError(
            "excluded-sql", {"secret": "excluded-secret"}, Exception("excluded-driver")
        )

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with pytest.raises(owner.StaffProvisioningError) as error:
        owner.delete_staff_account(db_session, command=command(user_id))
    assert (
        error.value.code.endswith(".deletion_unavailable")
        and error.value.__suppress_context__
    )
    assert "excluded" not in str(error.value) and error.value.__cause__ is None
    assert (
        db_session.get(ApiKey, key_id) is not None
        and db_session.get(SystemUser, user_id) is not None
    )


def test_recovery_code_remains_atomic_linked_record_blocker(db_session):
    user_id, key_id = staff_fixture(db_session)
    method = db_session.scalar(
        select(MFAMethod).where(MFAMethod.system_user_id == user_id)
    )
    code = MFARecoveryCode(mfa_method_id=method.id, code_hash="inert-recovery-hash")
    db_session.add(code)
    db_session.commit()
    with pytest.raises(owner.StaffProvisioningError) as error:
        owner.delete_staff_account(db_session, command=command(user_id))
    assert error.value.code.endswith(".linked_records")
    assert (
        db_session.get(ApiKey, key_id) is not None
        and db_session.get(SystemUser, user_id) is not None
    )
    assert not db_session.scalars(select(AuditEvent)).all()


def test_single_adapter_fresh_session_and_redirects(db_session, monkeypatch):
    user_id, _ = staff_fixture(db_session)
    seen = []

    @contextmanager
    def fresh():
        seen.append("open")
        yield db_session
        assert not db_session.in_transaction()
        seen.append("close")

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    response = system.user_delete(request_fixture(), str(user_id), db_session)
    assert (
        response.status_code == 303
        and response.headers["location"] == "/admin/system/users"
    )
    assert seen == ["open", "close"]


def test_bulk_adapter_uses_trusted_request_and_counts(db_session, monkeypatch):
    user_id, _ = staff_fixture(db_session)

    @contextmanager
    def fresh():
        yield db_session

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    result = system.users_bulk_delete(
        request_fixture(), {"user_ids": [str(user_id), str(user_id)]}, db_session
    )
    assert result["deleted_count"] == 1 and result["skipped_count"] == 1


def test_party_history_is_retained_and_dual_bound_key_is_deleted(
    db_session, subscriber
):
    from app.models.party import Party

    user_id, key_id = staff_fixture(db_session)
    party = Party(party_type="person", display_name="retained native identity")
    db_session.add(party)
    db_session.flush()
    party_id = party.id
    bind_party_fixture(db_session, user_id=user_id, party_id=party_id)
    db_session.get(ApiKey, key_id).subscriber_id = subscriber.id
    subscriber_id = subscriber.id
    db_session.commit()
    result = owner.delete_staff_account(db_session, command=command(user_id))
    assert result.deleted_key_ids == (key_id,)
    assert db_session.get(Party, party_id) is not None
    from app.models.subscriber import Subscriber

    assert db_session.get(Subscriber, subscriber_id) is not None


@pytest.mark.parametrize("denial", ["permission", "actor", "admin_mount"])
@pytest.mark.parametrize("bulk", [False, True])
def test_real_http_deletion_guard_chain_denies_before_owner(
    db_session, monkeypatch, denial, bulk
):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.models.rbac import Permission
    from app.models.subscriber import UserType
    from app.services import auth_dependencies, erp_staff_access
    from app.web.auth.dependencies import require_admin_web_auth, require_web_auth

    db_session.add(Permission(key="rbac:assign", is_active=True))
    db_session.commit()
    auth = {
        "principal_type": "subscriber" if denial == "admin_mount" else "system_user",
        "subscriber": SystemUser(user_type=UserType.system_user),
        "roles": ["support"],
        "scopes": [] if denial == "permission" else ["rbac:assign"],
    }
    app = FastAPI()
    app.include_router(
        system.router, prefix="/admin", dependencies=[Depends(require_admin_web_auth)]
    )
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_web_auth] = lambda: dict(auth)
    app.dependency_overrides[auth_dependencies.require_user_auth] = lambda: dict(auth)
    monkeypatch.setattr(
        auth_dependencies, "load_permission_keys", lambda auth, db: frozenset()
    )
    monkeypatch.setattr(
        auth_dependencies, "has_permission", lambda auth, db, key: False
    )
    monkeypatch.setattr(
        auth_dependencies, "_enforce_staff_leave_write_guard", lambda **kwargs: None
    )
    monkeypatch.setattr(
        erp_staff_access, "staff_write_restricted", lambda *args, **kwargs: None
    )

    def forbidden(*args, **kwargs):
        pytest.fail("denied request entered owner/session")

    monkeypatch.setattr(db_session_adapter, "owner_command_session", forbidden)
    monkeypatch.setattr(owner, "delete_staff_account", forbidden)
    monkeypatch.setattr(owner, "bulk_delete_staff_accounts", forbidden)
    target = str(uuid4())
    client = TestClient(app, raise_server_exceptions=False)
    response = (
        client.post("/admin/system/users/bulk/delete", json={"user_ids": [target]})
        if bulk
        else client.delete(f"/admin/system/users/{target}")
    )
    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Forbidden"
        if denial == "permission"
        else "Authorized actor is missing"
        if denial == "actor"
        else "Administrator access is required for this area."
    )


@pytest.mark.parametrize("participant", ["keys", "grants"])
@pytest.mark.parametrize("wrong", ["command", "context"])
def test_participants_refuse_other_staff_command_or_context(
    db_session, participant, wrong
):
    from app.services.owner_commands import (
        OwnerCommandDefinition,
        execute_owner_command,
    )

    cmd = command(uuid4())
    definition = OwnerCommandDefinition(
        owner="auth.staff_provisioning",
        concern="staff account provisioning"
        if wrong == "command"
        else owner.STAFF_DELETION_CONCERN,
        name="set_staff_account_active"
        if wrong == "command"
        else "delete_staff_account",
    )
    supplied_context = (
        replace(cmd.context, command_id=uuid4()) if wrong == "context" else cmd.context
    )

    def operation():
        if participant == "keys":
            return keys.stage_staff_key_deletion(
                db_session,
                command=keys.StaffKeyDeletionCommand(
                    supplied_context, cmd.actor, (cmd.user_id,)
                ),
            )
        return grants.remove_all_for_system_user(
            db_session,
            command=grants.RemoveSystemUserAssignmentsCommand(
                supplied_context, cmd.user_id
            ),
        )

    with pytest.raises(DomainError, match="Trusted"):
        execute_owner_command(
            db_session, definition=definition, context=cmd.context, operation=operation
        )
    assert not db_session.in_transaction()


def test_deleted_staff_invite_cannot_materialize_new_capability(
    db_session, monkeypatch
):
    import hashlib

    from app.models.notification import Notification, NotificationChannel
    from app.services.ephemeral_communication_actions import (
        EPHEMERAL_ACTION_METADATA_KEY,
        STAFF_ACCOUNT_INVITE_ACTION,
        EphemeralActionRejected,
        materialize_email,
    )

    target, _ = staff_fixture(db_session)
    email = db_session.get(SystemUser, target).email
    notification = Notification(
        channel=NotificationChannel.email,
        audience_type="system_user",
        audience_id=target,
        recipient=email,
        metadata_={
            EPHEMERAL_ACTION_METADATA_KEY: {
                "type": STAFF_ACCOUNT_INVITE_ACTION,
                "version": 1,
                "context": {
                    "user_id": str(target),
                    "email_sha256": hashlib.sha256(email.encode()).hexdigest(),
                },
            }
        },
    )
    db_session.commit()
    owner.delete_staff_account(db_session, command=command(target))
    monkeypatch.setattr(
        owner.credential_recovery,
        "issue_exact_reset_capability",
        lambda *args, **kwargs: pytest.fail("deleted principal minted capability"),
    )
    with pytest.raises(EphemeralActionRejected, match="stale_account_context"):
        materialize_email(db_session, notification)
