"""Typed PATCH commands for migrated unit consumers."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.schemas.auth import ApiKeyUpdate
from app.services import api_key_administration as owner
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext


def update_command(key_id: UUID, payload: ApiKeyUpdate) -> owner.UpdateApiKeyCommand:
    actor = AuditActor.user(str(uuid4()))
    assert actor.actor_id is not None
    return owner.UpdateApiKeyCommand(
        context=CommandContext.system(
            actor=actor.actor_id, scope=owner.UPDATE_SCOPE, reason="unit fixture"
        ),
        key_id=key_id,
        actor=actor,
        fields=frozenset(
            owner.ApiKeyUpdateField(name) for name in payload.model_fields_set
        ),
        subscriber_id=payload.subscriber_id,
        label=payload.label,
        supplied_secret=payload.key_hash,
        scopes=tuple(payload.scopes) if payload.scopes is not None else None,
        is_active=payload.is_active,
        last_used_at=payload.last_used_at,
        expires_at=payload.expires_at,
        revoked_at=payload.revoked_at,
    )


def update(
    db: Session, key_id: str, payload: ApiKeyUpdate
) -> owner.ApiKeyUpdateOutcome:
    command = update_command(UUID(key_id), payload)
    db.commit()  # End fixture setup/reads before invoking the command owner.
    return owner.update(db, command=command)
