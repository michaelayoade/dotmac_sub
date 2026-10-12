"""Typed, audited owner of the administrative JSON API-key revocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import ApiKey
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "auth.api_key_administration"
CONCERN = "administrative JSON API-key revocation"
SCOPE = "api_key:revoke"
_DEFINITION = OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="revoke")


class RevocationProvenance(StrEnum):
    AUTHENTICATED_ADMINISTRATOR = "authenticated_administrator"


@dataclass(frozen=True, slots=True)
class RevokeApiKeyCommand:
    context: CommandContext
    key_id: UUID
    actor: AuditActor
    provenance: RevocationProvenance = field(
        default=RevocationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
    )


@dataclass(frozen=True, slots=True)
class ApiKeyRevocationOutcome:
    key_id: UUID
    revoked_at: datetime


class ApiKeyRevocationError(DomainError):
    """Transport-neutral refusal of administrative revocation."""


def revoke(db: Session, *, command: RevokeApiKeyCommand) -> ApiKeyRevocationOutcome:
    """Stamp each successful invocation; state and canonical audit commit together.

    Repeated revocation deliberately stamps a fresh time and audit event, matching
    the retired API writer. There is no durable replay or automatic retry.
    """

    if (
        not isinstance(command, RevokeApiKeyCommand)
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or command.provenance is not RevocationProvenance.AUTHENTICATED_ADMINISTRATOR
    ):
        raise ApiKeyRevocationError(
            code=f"{OWNER}.invalid_actor",
            message="Trusted administrative actor required.",
        )

    def operation() -> ApiKeyRevocationOutcome:
        if (
            not isinstance(command.key_id, UUID)
            or command.actor.actor_type
            not in {AuditActorType.user, AuditActorType.api_key}
            or command.context.actor != command.actor.actor_id
            or command.context.scope != SCOPE
        ):
            raise ApiKeyRevocationError(
                code=f"{OWNER}.invalid_actor",
                message="Trusted administrative actor required.",
            )
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
        key = db.scalars(
            select(ApiKey)
            .where(ApiKey.id == command.key_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if key is None:
            raise ApiKeyRevocationError(
                code=f"{OWNER}.not_found", message="API key not found"
            )
        previous_active = key.is_active
        previous_revoked_at = key.revoked_at
        revoked_at = datetime.now(UTC)
        key.is_active = False
        key.revoked_at = revoked_at
        db.flush()
        stage_audit_event(
            db,
            action="auth.api_key.revoked",
            entity_type="api_key",
            entity_id=str(key.id),
            actor=AuditActor(command.actor.actor_type, command.actor.actor_id),
            metadata={
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
            },
            details={
                "old_is_active": previous_active,
                "old_revoked_at": previous_revoked_at.isoformat()
                if previous_revoked_at
                else None,
                "is_active": False,
                "revoked_at": revoked_at.isoformat(),
            },
        )
        return ApiKeyRevocationOutcome(key_id=key.id, revoked_at=revoked_at)

    return execute_owner_command(
        db, definition=_DEFINITION, context=command.context, operation=operation
    )
