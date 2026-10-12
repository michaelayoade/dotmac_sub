"""Typed deletion fixtures with inert credential material only."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.auth import (
    ApiKey,
    MFAMethod,
    MFAMethodType,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.system_user import SystemUser
from app.services import staff_provisioning as owner
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext


def command(user_id: UUID) -> owner.DeleteStaffAccountCommand:
    actor = AuditActor.user(str(uuid4()))
    context = CommandContext.system(
        actor=f"user:{actor.actor_id}",
        scope=owner.STAFF_ASSIGN_SCOPE,
        reason="delete inactive staff fixture",
    )
    return owner.DeleteStaffAccountCommand(context, user_id, actor)


def bulk_command(user_ids: tuple[UUID, ...]) -> owner.BulkDeleteStaffAccountsCommand:
    single = command(uuid4())
    return owner.BulkDeleteStaffAccountsCommand(single.context, user_ids, single.actor)


def staff_fixture(
    db: Session, *, active: bool = False, auth_children: bool = True
) -> tuple[UUID, UUID | None]:
    user = SystemUser(
        first_name="Excluded",
        last_name="Identity",
        email=f"{uuid4()}@test.invalid",
        is_active=active,
    )
    db.add(user)
    db.flush()
    user_id = user.id
    key_id = None
    if auth_children:
        key = ApiKey(
            system_user_id=user_id,
            key_hash=f"inert-key-{uuid4()}",
            label="excluded-key-label",
            scopes=["excluded:scope"],
        )
        db.add_all(
            [
                key,
                UserCredential(
                    system_user_id=user_id,
                    username=f"inert-{uuid4()}",
                    password_hash="inert-password-hash",
                ),
                AuthSession(
                    system_user_id=user_id,
                    status=SessionStatus.revoked,
                    token_hash=f"inert-session-{uuid4()}",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                ),
                MFAMethod(
                    system_user_id=user_id,
                    method_type=MFAMethodType.totp,
                    secret="inert-seed",
                ),
            ]
        )
        db.flush()
        key_id = key.id
    db.commit()
    return user_id, key_id


def bind_party_fixture(db: Session, *, user_id: UUID, party_id: UUID) -> None:
    from datetime import UTC, datetime

    user = db.get(SystemUser, user_id)
    assert user is not None
    user.person_party_id = party_id
    user.party_bound_at = datetime.now(UTC)
    user.party_binding_source = "pytest.staff-deletion"
    user.party_binding_reason = "Verified inert deletion fixture identity"
    db.flush()
