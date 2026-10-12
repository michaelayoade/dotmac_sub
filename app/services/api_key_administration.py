"""Typed, audited owner of the administrative JSON API-key issuance, update and revocation."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import ApiKey
from app.models.subscriber import Subscriber
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


class AdministrationProvenance(StrEnum):
    AUTHENTICATED_ADMINISTRATOR = "authenticated_administrator"


@dataclass(frozen=True, slots=True)
class RevokeApiKeyCommand:
    context: CommandContext
    key_id: UUID
    actor: AuditActor
    provenance: AdministrationProvenance = field(
        default=AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
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
        or command.provenance
        is not AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR
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


ISSUANCE_CONCERN = "administrative JSON API-key issuance"
CREATE_SCOPE = "api_key:create"
GENERATE_SCOPE = "api_key:generate"
_CREATE_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=ISSUANCE_CONCERN, name="create"
)
_GENERATE_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=ISSUANCE_CONCERN, name="generate"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateApiKeyCommand:
    context: CommandContext
    actor: AuditActor = field(repr=False)
    supplied_secret: str = field(repr=False)
    subscriber_id: UUID | None = None
    label: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ()
    is_active: bool = True
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    provenance: AdministrationProvenance = field(
        default=AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerateApiKeyCommand:
    context: CommandContext
    actor: AuditActor = field(repr=False)
    subscriber_id: UUID | None = None
    label: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    provenance: AdministrationProvenance = field(
        default=AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
    )


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    id: UUID
    subscriber_id: UUID | None
    label: str | None = field(repr=False)
    key_hash: str = field(repr=False)
    scopes: tuple[str, ...]
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApiKeyGenerationOutcome:
    api_key: IssuedApiKey
    key: str = field(repr=False)


class ApiKeyIssuanceError(DomainError):
    """Transport-neutral refusal of administrative issuance."""


def _validate_issuance_actor(
    command: CreateApiKeyCommand | GenerateApiKeyCommand, scope: str
) -> None:
    if (
        not isinstance(command, (CreateApiKeyCommand, GenerateApiKeyCommand))
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or command.provenance
        is not AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR
        or command.actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or command.context.actor != command.actor.actor_id
        or command.context.scope != scope
    ):
        raise ApiKeyIssuanceError(
            code=f"{OWNER}.invalid_actor",
            message="Trusted administrative actor required.",
        )


def _stage_issuance(
    db: Session,
    *,
    command: CreateApiKeyCommand | GenerateApiKeyCommand,
    secret: str,
    generated: bool,
) -> IssuedApiKey:
    from app.services.auth import hash_api_key

    if (
        (
            command.subscriber_id is not None
            and not isinstance(command.subscriber_id, UUID)
        )
        or (
            command.label is not None
            and (not isinstance(command.label, str) or len(command.label) > 120)
        )
        or not isinstance(command.scopes, tuple)
        or any(not isinstance(scope, str) for scope in command.scopes)
        or not isinstance(secret, str)
        or not secret
        or (not generated and len(secret) > 255)
        or any(
            value is not None and not isinstance(value, datetime)
            for value in (
                command.expires_at,
                command.last_used_at
                if isinstance(command, CreateApiKeyCommand)
                else None,
                command.revoked_at
                if isinstance(command, CreateApiKeyCommand)
                else None,
            )
        )
        or (
            isinstance(command, CreateApiKeyCommand)
            and not isinstance(command.is_active, bool)
        )
    ):
        raise ApiKeyIssuanceError(
            code=f"{OWNER}.invalid_command", message="Invalid API-key issuance command."
        )
    if command.subscriber_id is not None:
        # Match the legacy existence policy; the FK remains the deletion-race backstop.
        if db.get(Subscriber, command.subscriber_id) is None:
            raise ApiKeyIssuanceError(
                code=f"{OWNER}.subscriber_not_found", message="Subscriber not found"
            )
    key = ApiKey(
        subscriber_id=command.subscriber_id,
        label=command.label,
        key_hash=hash_api_key(secret),
        scopes=list(command.scopes),
        expires_at=command.expires_at,
        is_active=command.is_active
        if isinstance(command, CreateApiKeyCommand)
        else True,
        last_used_at=command.last_used_at
        if isinstance(command, CreateApiKeyCommand)
        else None,
        revoked_at=command.revoked_at
        if isinstance(command, CreateApiKeyCommand)
        else None,
    )
    db.add(key)
    db.flush()
    stage_audit_event(
        db,
        action="auth.api_key.generated" if generated else "auth.api_key.created",
        entity_type="api_key",
        entity_id=str(key.id),
        actor=AuditActor(command.actor.actor_type, command.actor.actor_id),
        metadata={
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        },
        details={"is_active": key.is_active},
    )
    return IssuedApiKey(
        id=key.id,
        subscriber_id=key.subscriber_id,
        label=key.label,
        key_hash=key.key_hash,
        scopes=tuple(key.scopes),
        is_active=key.is_active,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
    )


def create(db: Session, *, command: CreateApiKeyCommand) -> IssuedApiKey:
    _validate_issuance_actor(command, CREATE_SCOPE)
    if not isinstance(command, CreateApiKeyCommand):
        raise ApiKeyIssuanceError(
            code=f"{OWNER}.invalid_command", message="Invalid API-key issuance command."
        )
    try:
        return execute_owner_command(
            db,
            definition=_CREATE_DEFINITION,
            context=command.context,
            operation=lambda: _stage_issuance(
                db, command=command, secret=command.supplied_secret, generated=False
            ),
        )
    except SQLAlchemyError:
        raise ApiKeyIssuanceError(
            code=f"{OWNER}.storage_unavailable", message="API-key issuance unavailable."
        ) from None


def generate(db: Session, *, command: GenerateApiKeyCommand) -> ApiKeyGenerationOutcome:
    _validate_issuance_actor(command, GENERATE_SCOPE)
    if not isinstance(command, GenerateApiKeyCommand):
        raise ApiKeyIssuanceError(
            code=f"{OWNER}.invalid_command", message="Invalid API-key issuance command."
        )

    def operation() -> ApiKeyGenerationOutcome:
        raw_key = secrets.token_urlsafe(32)
        issued = _stage_issuance(db, command=command, secret=raw_key, generated=True)
        return ApiKeyGenerationOutcome(api_key=issued, key=raw_key)

    try:
        return execute_owner_command(
            db,
            definition=_GENERATE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except SQLAlchemyError:
        raise ApiKeyIssuanceError(
            code=f"{OWNER}.storage_unavailable", message="API-key issuance unavailable."
        ) from None


UPDATE_CONCERN = "administrative JSON API-key update"
UPDATE_SCOPE = "api_key:update"
_UPDATE_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=UPDATE_CONCERN, name="update"
)


class ApiKeyUpdateField(StrEnum):
    SUBSCRIBER_ID = "subscriber_id"
    LABEL = "label"
    KEY_HASH = "key_hash"
    SCOPES = "scopes"
    IS_ACTIVE = "is_active"
    LAST_USED_AT = "last_used_at"
    EXPIRES_AT = "expires_at"
    REVOKED_AT = "revoked_at"


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateApiKeyCommand:
    context: CommandContext
    key_id: UUID
    actor: AuditActor = field(repr=False)
    fields: frozenset[ApiKeyUpdateField]
    subscriber_id: UUID | None = None
    label: str | None = field(default=None, repr=False)
    supplied_secret: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] | None = field(default=None, repr=False)
    is_active: bool | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    provenance: AdministrationProvenance = field(
        default=AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
    )


@dataclass(frozen=True, slots=True)
class ApiKeyUpdateOutcome:
    id: UUID
    subscriber_id: UUID | None
    system_user_id: UUID | None
    label: str | None = field(repr=False)
    key_hash: str | None = field(repr=False)
    scopes: tuple[str, ...] | None = field(repr=False)
    is_active: bool | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyUpdateError(DomainError):
    """Transport-neutral refusal of administrative JSON update."""


def _validate_update(command: UpdateApiKeyCommand) -> None:
    if (
        not isinstance(command, UpdateApiKeyCommand)
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or command.provenance
        is not AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR
        or command.actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or command.context.actor != command.actor.actor_id
        or command.context.scope != UPDATE_SCOPE
    ):
        raise ApiKeyUpdateError(
            code=f"{OWNER}.invalid_actor",
            message="Trusted administrative actor required.",
        )
    if (
        not isinstance(command.key_id, UUID)
        or not isinstance(command.fields, frozenset)
        or any(not isinstance(value, ApiKeyUpdateField) for value in command.fields)
        or (
            command.subscriber_id is not None
            and not isinstance(command.subscriber_id, UUID)
        )
        or (
            command.label is not None
            and (not isinstance(command.label, str) or len(command.label) > 120)
        )
        or (
            command.supplied_secret is not None
            and (
                not isinstance(command.supplied_secret, str)
                or not 1 <= len(command.supplied_secret) <= 255
            )
        )
        or (
            command.scopes is not None
            and (
                not isinstance(command.scopes, tuple)
                or any(not isinstance(value, str) for value in command.scopes)
            )
        )
        or (command.is_active is not None and not isinstance(command.is_active, bool))
        or any(
            value is not None and not isinstance(value, datetime)
            for value in (command.last_used_at, command.expires_at, command.revoked_at)
        )
    ):
        raise ApiKeyUpdateError(
            code=f"{OWNER}.invalid_command", message="Invalid API-key update command."
        )


def update(db: Session, *, command: UpdateApiKeyCommand) -> ApiKeyUpdateOutcome:
    """Preserve PATCH field/null semantics while atomically auditing each invocation."""
    _validate_update(command)

    def operation() -> ApiKeyUpdateOutcome:
        from app.services.auth import hash_api_key

        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
        key = db.scalars(
            select(ApiKey)
            .where(ApiKey.id == command.key_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if key is None:
            raise ApiKeyUpdateError(
                code=f"{OWNER}.not_found", message="API key not found"
            )
        if (
            ApiKeyUpdateField.SUBSCRIBER_ID in command.fields
            and command.subscriber_id is not None
        ):
            if db.get(Subscriber, command.subscriber_id) is None:
                raise ApiKeyUpdateError(
                    code=f"{OWNER}.subscriber_not_found", message="Subscriber not found"
                )
        previous_hash = key.key_hash
        # Explicit assignments preserve legacy exclude_unset semantics. Nullable
        # command values intentionally reach persistence, including constraint failures.
        if ApiKeyUpdateField.SUBSCRIBER_ID in command.fields:
            key.subscriber_id = command.subscriber_id
        if ApiKeyUpdateField.LABEL in command.fields:
            key.label = command.label
        if ApiKeyUpdateField.KEY_HASH in command.fields:
            key.key_hash = (
                hash_api_key(command.supplied_secret)
                if command.supplied_secret
                else cast(str, command.supplied_secret)
            )
        if ApiKeyUpdateField.SCOPES in command.fields:
            # JSON(none_as_null=False) stores None as JSON null, not SQL NULL.
            key.scopes = (
                list(command.scopes)
                if command.scopes is not None
                else cast(list[str], None)
            )
        if ApiKeyUpdateField.IS_ACTIVE in command.fields:
            key.is_active = cast(bool, command.is_active)
        if ApiKeyUpdateField.LAST_USED_AT in command.fields:
            key.last_used_at = command.last_used_at
        if ApiKeyUpdateField.EXPIRES_AT in command.fields:
            key.expires_at = command.expires_at
        if ApiKeyUpdateField.REVOKED_AT in command.fields:
            key.revoked_at = command.revoked_at
        db.flush()
        stage_audit_event(
            db,
            action="auth.api_key.updated",
            entity_type="api_key",
            entity_id=str(key.id),
            actor=AuditActor(command.actor.actor_type, command.actor.actor_id),
            metadata={
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
            },
            details={
                "updated_fields": sorted(value.value for value in command.fields),
                "hash_changed": previous_hash != key.key_hash,
            },
        )
        return ApiKeyUpdateOutcome(
            id=key.id,
            subscriber_id=key.subscriber_id,
            system_user_id=key.system_user_id,
            label=key.label,
            key_hash=key.key_hash,
            scopes=tuple(key.scopes) if key.scopes is not None else None,
            is_active=key.is_active,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
        )

    try:
        return execute_owner_command(
            db,
            definition=_UPDATE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except OperationalError as exc:
        if (
            getattr(exc.orig, "sqlstate", None) == "55P03"
            or getattr(exc.orig, "pgcode", None) == "55P03"
        ):
            raise ApiKeyUpdateError(
                code=f"{OWNER}.busy", message="API-key update busy."
            ) from None
        raise ApiKeyUpdateError(
            code=f"{OWNER}.storage_unavailable", message="API-key update unavailable."
        ) from None
    except SQLAlchemyError:
        raise ApiKeyUpdateError(
            code=f"{OWNER}.storage_unavailable", message="API-key update unavailable."
        ) from None
