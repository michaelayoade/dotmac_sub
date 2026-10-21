"""Typed web-owner commands for unit and hosted PostgreSQL consumers."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.services import api_key_administration as owner
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext


def context(scope: str) -> tuple[CommandContext, AuditActor]:
    actor = AuditActor.user(str(uuid4()))
    return CommandContext.system(
        actor=f"user:{actor.actor_id}", scope=scope, reason="unit fixture"
    ), actor


def create_command(
    *,
    subscriber_id: str | None,
    label: str,
    expires_in: str | None,
    scopes: list[str] | None = None,
) -> owner.WebCreateApiKeyCommand:
    ctx, actor = context(owner.WEB_CREATE_SCOPE)
    return owner.WebCreateApiKeyCommand(
        context=ctx,
        actor=actor,
        subscriber_id=UUID(subscriber_id) if subscriber_id else None,
        label=label,
        expires_in_days=int(expires_in) if expires_in else None,
        scopes=tuple(scopes or ()),
    )


def mutate_command(
    key_id: UUID,
    action: owner.WebApiKeyAction,
    subscriber_id: UUID | None = None,
    *,
    invalid: bool = False,
) -> owner.WebMutateApiKeyCommand:
    ctx, actor = context(
        owner.WEB_REVOKE_SCOPE
        if action is owner.WebApiKeyAction.REVOKE
        else owner.WEB_ROTATE_SCOPE
    )
    return owner.WebMutateApiKeyCommand(
        context=ctx,
        actor=actor,
        key_id=key_id,
        caller_scope=owner.WebApiKeyCallerScope(subscriber_id, invalid),
        action=action,
    )


def create_api_key(
    db: Session,
    *,
    subscriber_id: str | None,
    label: str,
    expires_in: str | None,
    scopes: list[str] | None = None,
) -> str:
    command = create_command(
        subscriber_id=subscriber_id, label=label, expires_in=expires_in, scopes=scopes
    )
    db.commit()
    secret = owner.web_create(db, command=command).secret
    assert secret is not None
    return secret


def revoke_api_key(db: Session, *, key_id: str) -> bool:
    key = db.get(ApiKey, UUID(key_id))
    command = mutate_command(
        UUID(key_id), owner.WebApiKeyAction.REVOKE, key.subscriber_id if key else None
    )
    db.commit()
    return owner.web_mutate(db, command=command).key_id is not None


def rotate_api_key(db: Session, *, key_id: str) -> str | None:
    key = db.get(ApiKey, UUID(key_id))
    command = mutate_command(
        UUID(key_id), owner.WebApiKeyAction.ROTATE, key.subscriber_id if key else None
    )
    db.commit()
    return owner.web_mutate(db, command=command).secret
