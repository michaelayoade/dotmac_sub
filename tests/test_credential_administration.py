"""Non-authoritative unit evidence for credential administration commands."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.models.subscriber import SubscriberCategory
from app.schemas.auth import UserCredentialCreate, UserCredentialUpdate
from app.services import credential_administration as owner
from app.services import credential_administration_mutation as mutation
from app.services.auth_flow import hash_password, verify_user_password
from app.services.owner_commands import CommandContext

PASSWORD = "Strong-Test-Password-42!"


def context():
    return CommandContext.system(
        actor="service:credential-admin-test",
        scope=owner.SCOPE,
        reason="verify administrative transition",
    )


def create(db, subscriber, **kwargs):
    identity = subscriber.id
    db.commit()  # Test fixture refresh is an explicit completed read transaction.
    return owner.create_credential(
        db,
        command=owner.CreateCredential(
            context=context(),
            subscriber_id=identity,
            username="admin-created",
            password=PASSWORD,
            **kwargs,
        ),
    )


@pytest.mark.parametrize(
    "field",
    [
        "password_hash",
        "credential_version",
        "must_change_password",
        "is_active",
        "locked_until",
        "password_updated_at",
        "subscriber_id",
        "provider",
        "system_user_id",
        "party_id",
        "token_hash",
    ],
)
def test_patch_forbids_standing_and_identity_fields(field):
    with pytest.raises(ValidationError):
        UserCredentialUpdate.model_validate({"username": "valid", field: "forbidden"})


def test_create_forbids_raw_hash_and_accepts_person_alias():
    with pytest.raises(ValidationError):
        UserCredentialCreate.model_validate(
            {
                "person_id": str(uuid4()),
                "username": "valid",
                "password": PASSWORD,
                "password_hash": "forbidden",
            }
        )
    assert (
        UserCredentialCreate(person_id=uuid4(), password=PASSWORD).password == PASSWORD
    )
    assert PASSWORD not in repr(
        UserCredentialCreate(person_id=uuid4(), password=PASSWORD)
    )


def test_create_hashes_and_emits_only_safe_evidence(db_session, subscriber):
    result = create(db_session, subscriber)
    row = db_session.get(UserCredential, result.credential.id)
    assert row.credential_version == 1
    assert verify_user_password(PASSWORD, row.password_hash)
    assert "password_hash" not in repr(result)
    evidence = repr(db_session.query(EventStore).all()[0].payload) + repr(
        db_session.query(AuditEvent).all()[0].metadata_
    )
    assert PASSWORD not in evidence
    assert row.password_hash not in evidence


def test_password_reset_revokes_all_principal_sessions_preserves_inactive(
    db_session, subscriber
):
    result = create(db_session, subscriber)
    row = db_session.get(UserCredential, result.credential.id)
    row.is_active = False
    row.failed_login_attempts = 7
    row.must_change_password = True
    row.locked_until = datetime(2099, 1, 1, tzinfo=UTC)
    session = AuthSession(
        subscriber_id=row.subscriber_id,
        token_hash="test-session",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db_session.add(session)
    db_session.commit()
    identifier = result.credential.id
    db_session.commit()
    out = owner.update_credential(
        db_session,
        command=owner.UpdateCredential(
            context(),
            identifier,
            frozenset({owner.CredentialField.password}),
            password=PASSWORD,
        ),
    )
    assert out.credential_version == 2
    assert not out.credential.is_active
    assert out.credential.failed_login_attempts == 0
    assert not out.credential.must_change_password
    assert out.credential.locked_until is None
    assert int(out.credential.password_updated_at.timestamp()) > int(
        datetime.now(UTC).timestamp()
    )
    db_session.expire_all()
    assert db_session.get(AuthSession, session.id).status == SessionStatus.revoked


def test_username_version_deactivation_and_metadata_noop(db_session, subscriber):
    result = create(db_session, subscriber)
    changed = owner.update_credential(
        db_session,
        command=owner.UpdateCredential(
            context(),
            result.credential.id,
            frozenset({owner.CredentialField.username}),
            username="changed",
        ),
    )
    assert changed.credential_version == 2
    same = owner.update_credential(
        db_session,
        command=owner.UpdateCredential(context(), result.credential.id, frozenset()),
    )
    assert same.credential_version == 2 and not same.changed
    off = owner.deactivate_credential(
        db_session, command=owner.DeactivateCredential(context(), result.credential.id)
    )
    again = owner.deactivate_credential(
        db_session, command=owner.DeactivateCredential(context(), result.credential.id)
    )
    assert off.credential_version == again.credential_version == 3
    assert not again.changed


@pytest.mark.parametrize("password", [None, "", "short"])
def test_local_password_cannot_be_cleared(db_session, subscriber, password):
    result = create(db_session, subscriber)
    with pytest.raises(
        owner.CredentialAdministrationError, match="password|characters"
    ):
        owner.update_credential(
            db_session,
            command=owner.UpdateCredential(
                context(),
                result.credential.id,
                frozenset({owner.CredentialField.password}),
                password=password,
            ),
        )
    assert not db_session.in_transaction()


def test_default_provider_resolves_before_local_policy(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(owner, "resolve_value", lambda *args: "radius")
    identifier = subscriber.id
    db_session.commit()
    out = owner.create_credential(
        db_session, command=owner.CreateCredential(context(), identifier, "radius")
    )
    assert out.credential.provider == AuthProvider.radius


def test_no_email_principal_can_receive_admin_password(db_session, subscriber):
    subscriber.email = ""
    db_session.commit()
    assert create(db_session, subscriber).credential_version == 1


def test_flushed_caller_write_is_rejected_without_discard(db_session, subscriber):
    subscriber.notes = "caller flushed write"
    identifier = subscriber.id
    db_session.flush()
    with pytest.raises(owner.CredentialAdministrationError, match="transaction-free"):
        owner.create_credential(
            db_session,
            command=owner.CreateCredential(
                context(), identifier, "admin-created", password=PASSWORD
            ),
        )
    assert subscriber.notes == "caller flushed write"
    assert db_session.in_transaction()


@pytest.mark.parametrize("boundary", ["stage_audit_event", "emit_event"])
def test_audit_or_outbox_failure_rolls_back_credential(
    monkeypatch, db_session, subscriber, boundary
):
    identifier = subscriber.id
    db_session.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("durable evidence failure")

    monkeypatch.setattr(mutation, boundary, fail)
    with pytest.raises(RuntimeError, match="durable evidence"):
        owner.create_credential(
            db_session,
            command=owner.CreateCredential(
                context(), identifier, "rollback", password=PASSWORD
            ),
        )
    assert db_session.query(UserCredential).count() == 0
    assert db_session.query(EventStore).count() == 0
    assert db_session.query(AuditEvent).count() == 0


@pytest.mark.parametrize("boundary", ["stage_audit_event", "emit_event"])
def test_provisioning_metadata_and_login_roll_back_together(
    monkeypatch, db_session, subscriber, boundary
):
    identifier = subscriber.id
    original = subscriber.notes
    db_session.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("credential audit failed")

    monkeypatch.setattr(mutation, boundary, fail)
    with pytest.raises(RuntimeError):
        owner.provision_subscriber_login(
            db_session,
            command=owner.ProvisionSubscriberLogin(
                context(),
                identifier,
                False,
                "SUB-atomic",
                SubscriberCategory.residential,
                "new notes",
                True,
                "atomic-login",
                PASSWORD,
            ),
        )
    db_session.refresh(subscriber)
    assert subscriber.notes == original
    assert db_session.query(UserCredential).count() == 0
    assert db_session.query(EventStore).count() == 0


def test_admin_reset_invalidates_same_second_recovery_capability(
    monkeypatch, db_session, subscriber
):
    from app.services import credential_recovery

    identifier = subscriber.id
    email = subscriber.email
    result = create(db_session, subscriber)
    prior = db_session.get(UserCredential, result.credential.id)
    prior.password_updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    db_session.commit()
    issued_at = int(datetime.now(UTC).timestamp())
    owner.update_credential(
        db_session,
        command=owner.UpdateCredential(
            context(),
            result.credential.id,
            frozenset({owner.CredentialField.password}),
            password=PASSWORD,
        ),
    )
    monkeypatch.setattr(
        credential_recovery,
        "_decode_capability",
        lambda *args: {
            "principal_type": "subscriber",
            "principal_id": str(identifier),
            "email": email,
            "iat": issued_at,
        },
    )
    with pytest.raises(credential_recovery.CredentialRecoveryError) as exc:
        credential_recovery.complete_password_reset(
            db_session,
            credential_recovery.CompletePasswordResetCommand(
                CommandContext.system(
                    actor="service:credential-admin-test",
                    scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                    reason="verify old recovery token refusal",
                ),
                "old-token",
                PASSWORD,
            ),
        )
    assert exc.value.code.endswith("invalid_reset_capability")


def test_admin_reset_invalidates_old_mfa_version(db_session, subscriber):
    from app.services import password_authentication as authentication

    result = create(db_session, subscriber)
    owner.update_credential(
        db_session,
        command=owner.UpdateCredential(
            context(),
            result.credential.id,
            frozenset({owner.CredentialField.password}),
            password=PASSWORD,
        ),
    )
    challenge = authentication.Challenge(
        typ="mfa",
        principal_id=str(result.credential.subscriber_id),
        principal_type="subscriber",
        party_id=None,
        audience="customer",
        src=authentication.SRC_USER_CREDENTIAL,
        credential_id=result.credential.id,
        credential_version=1,
        access_credential_id=None,
        access_credential_updated_us=None,
    )
    with pytest.raises(authentication.PasswordAuthenticationError):
        authentication._gate_challenge_standing(
            db_session, challenge, datetime.now(UTC)
        )
    assert db_session.get(UserCredential, result.credential.id).last_login_at is None


def test_staff_update_uses_staff_password_floor(db_session, monkeypatch):
    from app.models.system_user import SystemUser
    from app.services import auth_flow

    real_policy = auth_flow.password_min_length_for

    def configured_staff_policy(db, principal_type):
        assert principal_type == "system_user"
        return max(real_policy(db, principal_type), 16)

    monkeypatch.setattr(auth_flow, "password_min_length_for", configured_staff_policy)
    user = SystemUser(
        first_name="Staff", last_name="Test", email="staff-admin-floor@example.test"
    )
    db_session.add(user)
    db_session.flush()
    row = UserCredential(
        system_user_id=user.id,
        username=user.email,
        provider=AuthProvider.local,
        password_hash=hash_password(PASSWORD),
    )
    db_session.add(row)
    db_session.flush()
    identifier = row.id
    db_session.commit()
    with pytest.raises(owner.CredentialAdministrationError) as exc:
        owner.update_credential(
            db_session,
            command=owner.UpdateCredential(
                context(),
                identifier,
                frozenset({owner.CredentialField.password}),
                password="Valid-42!",
            ),
        )
    assert exc.value.code.endswith("invalid_password")


def test_radius_metadata_preserves_version_counters_and_sessions(
    db_session, subscriber
):
    from app.models.radius import RadiusServer

    result = create(db_session, subscriber)
    row = db_session.get(UserCredential, result.credential.id)
    row.failed_login_attempts = 7
    row.must_change_password = True
    row.locked_until = datetime(2099, 1, 1, tzinfo=UTC)
    server = RadiusServer(name="admin-metadata-radius", host="127.0.0.2")
    db_session.add(server)
    db_session.flush()
    server_id = server.id
    db_session.commit()
    out = owner.update_credential(
        db_session,
        command=owner.UpdateCredential(
            context(),
            result.credential.id,
            frozenset({owner.CredentialField.radius_server_id}),
            radius_server_id=server_id,
        ),
    )
    assert out.credential_version == 1 and out.sessions_revoked == 0
    assert out.credential.failed_login_attempts == 7
    assert out.credential.must_change_password
    assert out.credential.locked_until is not None


def test_nonlocal_password_patch_is_rejected_even_when_null(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(owner, "resolve_value", lambda *args: "radius")
    identifier = subscriber.id
    db_session.commit()
    out = owner.create_credential(
        db_session,
        command=owner.CreateCredential(context(), identifier, "radius-patch"),
    )
    with pytest.raises(owner.CredentialAdministrationError) as exc:
        owner.update_credential(
            db_session,
            command=owner.UpdateCredential(
                context(),
                out.credential.id,
                frozenset({owner.CredentialField.password}),
                password=None,
            ),
        )
    assert exc.value.code.endswith("invalid_password")


@pytest.mark.parametrize(
    "password", ["Valid-42!" + "x" * 256, "Valid-42!" + chr(0xD800)]
)
def test_internal_password_admission_rejects_size_and_invalid_unicode_without_hashing(
    monkeypatch, db_session, subscriber, password
):
    from app.services import auth_flow

    identifier = subscriber.id
    db_session.commit()

    def unexpected_hash(*args):
        raise AssertionError("invalid plaintext reached the hasher")

    monkeypatch.setattr(auth_flow, "hash_password", unexpected_hash)
    with pytest.raises(owner.CredentialAdministrationError) as exc:
        owner.create_credential(
            db_session,
            command=owner.CreateCredential(
                context(), identifier, "invalid-secret", password=password
            ),
        )
    assert exc.value.code.endswith("invalid_password")
    assert password not in str(exc.value)
    assert not db_session.in_transaction()


def test_deactivate_refreshes_retained_inactive_credential(db_session, subscriber):
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    db_session.expire_on_commit = False
    result = create(db_session, subscriber)
    retained = db_session.get(UserCredential, result.credential.id)
    retained.is_active = False
    retained.credential_version = 2
    db_session.commit()
    with Session(
        bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
    ) as external:
        external.execute(
            update(UserCredential)
            .where(UserCredential.id == retained.id)
            .values(is_active=True, credential_version=3)
        )
        external.commit()
    assert not retained.is_active
    result = owner.deactivate_credential(
        db_session, command=owner.DeactivateCredential(context(), retained.id)
    )
    assert result.changed and not result.credential.is_active
    assert result.credential_version == 4


def test_provision_refreshes_retained_principal_identity(db_session, subscriber):
    from sqlalchemy.orm import Session

    from app.models.subscriber import Subscriber

    db_session.expire_on_commit = False
    identifier = subscriber.id
    db_session.commit()
    with Session(
        bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
    ) as external:
        current = external.get(Subscriber, identifier)
        current.category = SubscriberCategory.business
        external.commit()
    assert subscriber.category != SubscriberCategory.business
    with pytest.raises(owner.CredentialAdministrationError) as exc:
        owner.provision_subscriber_login(
            db_session,
            command=owner.ProvisionSubscriberLogin(
                context(),
                identifier,
                False,
                "SUB-stale",
                SubscriberCategory.residential,
                "should not persist",
            ),
        )
    assert exc.value.code.endswith("invalid_principal")
    db_session.refresh(subscriber)
    assert subscriber.category == SubscriberCategory.business
    assert subscriber.notes != "should not persist"


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_password_reset_failure_preserves_secret_version_and_sessions(
    monkeypatch, db_session, subscriber, failure
):
    result = create(db_session, subscriber)
    row = db_session.get(UserCredential, result.credential.id)
    previous_hash = row.password_hash
    session = AuthSession(
        subscriber_id=row.subscriber_id,
        token_hash="rollback-session",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db_session.add(session)
    db_session.flush()
    session_id = session.id
    db_session.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("protected transaction failure")

    if failure == "audit":
        monkeypatch.setattr(mutation, "stage_audit_event", fail)
    else:
        from sqlalchemy import event

        event.listen(db_session, "before_commit", fail)
    with pytest.raises(RuntimeError, match="protected transaction failure"):
        owner.update_credential(
            db_session,
            command=owner.UpdateCredential(
                context(),
                result.credential.id,
                frozenset({owner.CredentialField.password}),
                password="Changed-Password-42!",
            ),
        )
    db_session.expire_all()
    unchanged = db_session.get(UserCredential, result.credential.id)
    assert unchanged.password_hash == previous_hash
    assert unchanged.credential_version == 1
    assert db_session.get(AuthSession, session_id).status == SessionStatus.active
    assert db_session.query(EventStore).count() == 1
    assert db_session.query(AuditEvent).count() == 1


def test_radius_authority_change_versions_revokes_and_denies_old_challenge(
    db_session, subscriber
):
    from app.models.radius import RadiusServer
    from app.services import password_authentication as authentication

    identifier = subscriber.id
    first = RadiusServer(name="first-authority", host="127.0.0.2")
    second = RadiusServer(name="second-authority", host="127.0.0.3")
    db_session.add_all([first, second])
    db_session.flush()
    first_id, second_id = first.id, second.id
    db_session.commit()
    created = owner.create_credential(
        db_session,
        command=owner.CreateCredential(
            context(), identifier, "radius-authority", AuthProvider.radius, first_id
        ),
    )
    row = db_session.get(UserCredential, created.credential.id)
    row.failed_login_attempts = 7
    row.locked_until = datetime(2099, 1, 1, tzinfo=UTC)
    session = AuthSession(
        subscriber_id=identifier,
        token_hash="radius-authority-session",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db_session.add(session)
    db_session.flush()
    session_id = session.id
    db_session.commit()
    updated = owner.update_credential(
        db_session,
        command=owner.UpdateCredential(
            context(),
            created.credential.id,
            frozenset({owner.CredentialField.radius_server_id}),
            radius_server_id=second_id,
        ),
    )
    assert updated.credential_version == 2 and updated.sessions_revoked == 1
    assert updated.credential.failed_login_attempts == 7
    assert updated.credential.locked_until is not None
    db_session.expire_all()
    assert db_session.get(AuthSession, session_id).status == SessionStatus.revoked
    challenge = authentication.Challenge(
        typ="mfa",
        principal_id=str(identifier),
        principal_type="subscriber",
        party_id=None,
        audience="customer",
        src=authentication.SRC_USER_CREDENTIAL,
        credential_id=created.credential.id,
        credential_version=1,
        access_credential_id=None,
        access_credential_updated_us=None,
    )
    with pytest.raises(authentication.PasswordAuthenticationError):
        authentication._gate_challenge_standing(
            db_session, challenge, datetime.now(UTC)
        )


def test_old_principal_reset_capability_cannot_redeem_new_admin_credential(
    monkeypatch, db_session, subscriber
):
    from app.services import credential_recovery

    identifier, email = subscriber.id, subscriber.email
    old = UserCredential(
        subscriber_id=identifier,
        provider=AuthProvider.local,
        username="old-inactive-local",
        password_hash=hash_password(PASSWORD),
        password_updated_at=datetime(2020, 1, 1, tzinfo=UTC),
        is_active=False,
    )
    db_session.add(old)
    db_session.commit()
    issued_at = int(datetime.now(UTC).timestamp())
    created = owner.create_credential(
        db_session,
        command=owner.CreateCredential(
            context(), identifier, "new-admin-local", password=PASSWORD
        ),
    )
    assert created.credential_version == 1
    assert int(created.credential.password_updated_at.timestamp()) > issued_at
    monkeypatch.setattr(
        credential_recovery,
        "_decode_capability",
        lambda *args: {
            "principal_type": "subscriber",
            "principal_id": str(identifier),
            "email": email,
            "iat": issued_at,
        },
    )
    with pytest.raises(credential_recovery.CredentialRecoveryError) as exc:
        credential_recovery.complete_password_reset(
            db_session,
            credential_recovery.CompletePasswordResetCommand(
                CommandContext.system(
                    actor="service:credential-admin-test",
                    scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                    reason="verify old principal reset capability refusal",
                ),
                "old-principal-capability",
                "Replacement-Password-42!",
            ),
        )
    assert exc.value.code.endswith("invalid_reset_capability")
    assert db_session.get(UserCredential, created.credential.id).credential_version == 1
