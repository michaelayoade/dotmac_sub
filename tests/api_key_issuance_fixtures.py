"""Typed issuance fixtures for legacy read/update unit consumers."""

from uuid import uuid4

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.schemas.auth import ApiKeyCreate, ApiKeyGenerateRequest
from app.services import api_key_administration as owner
from app.services import auth
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext


def create_command(payload: ApiKeyCreate) -> owner.CreateApiKeyCommand:
    actor = AuditActor.user(str(uuid4()))
    assert actor.actor_id is not None
    return owner.CreateApiKeyCommand(
        context=CommandContext.system(
            actor=actor.actor_id, scope=owner.CREATE_SCOPE, reason="unit fixture"
        ),
        actor=actor,
        supplied_secret=payload.key_hash,
        subscriber_id=payload.subscriber_id,
        label=payload.label,
        scopes=tuple(payload.scopes),
        is_active=payload.is_active,
        last_used_at=payload.last_used_at,
        expires_at=payload.expires_at,
        revoked_at=payload.revoked_at,
    )


def generate_command(payload: ApiKeyGenerateRequest) -> owner.GenerateApiKeyCommand:
    actor = AuditActor.user(str(uuid4()))
    assert actor.actor_id is not None
    return owner.GenerateApiKeyCommand(
        context=CommandContext.system(
            actor=actor.actor_id, scope=owner.GENERATE_SCOPE, reason="unit fixture"
        ),
        actor=actor,
        subscriber_id=payload.subscriber_id,
        label=payload.label,
        scopes=tuple(payload.scopes),
        expires_at=payload.expires_at,
    )


def create(db: Session, payload: ApiKeyCreate) -> ApiKey:
    command = create_command(payload)
    db.commit()  # Finish fixture setup/reads before invoking the owner.
    issued = owner.create(db, command=command)
    row = db.get(ApiKey, issued.id)
    assert row is not None
    return row


def generate(db: Session, payload: ApiKeyGenerateRequest) -> tuple[ApiKey, str]:
    command = generate_command(payload)
    db.commit()
    outcome = owner.generate(db, command=command)
    row = db.get(ApiKey, outcome.api_key.id)
    assert row is not None
    return row, outcome.key


def generate_with_rate_limit(
    db: Session, payload: ApiKeyGenerateRequest, request: Request | None
) -> owner.ApiKeyGenerationOutcome:
    auth.api_keys.enforce_generation_rate_limit(db, request=request)
    command = generate_command(payload)
    db.commit()
    return owner.generate(db, command=command)
