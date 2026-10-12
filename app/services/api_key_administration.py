"""Typed owner of API-key administration and local authentication maintenance."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import Row, case, delete, false, func, or_, select, text, true
from sqlalchemy import update as sql_update
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
    current_command_context,
    current_command_definition,
    execute_owner_command,
    owner_command_active,
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


WEB_CONCERN = "administrative web API-key creation revocation and rotation"
WEB_CREATE_SCOPE = "api_key:web_create"
WEB_REVOKE_SCOPE = "api_key:web_revoke"
WEB_ROTATE_SCOPE = "api_key:web_rotate"
_WEB_CREATE_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=WEB_CONCERN, name="web_create"
)
_WEB_MUTATE_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=WEB_CONCERN, name="web_mutate"
)


class WebApiKeyAction(StrEnum):
    REVOKE = "revoke"
    ROTATE = "rotate"


@dataclass(frozen=True, slots=True)
class WebApiKeyCallerScope:
    subscriber_id: UUID | None
    invalid_subscriber_id: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class WebCreateApiKeyCommand:
    context: CommandContext
    actor: AuditActor = field(repr=False)
    subscriber_id: UUID | None
    label: str = field(repr=False)
    expires_in_days: int | None = None
    scopes: tuple[str, ...] = field(default=(), repr=False)
    provenance: AdministrationProvenance = field(
        default=AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class WebMutateApiKeyCommand:
    context: CommandContext
    actor: AuditActor = field(repr=False)
    key_id: UUID
    caller_scope: WebApiKeyCallerScope
    action: WebApiKeyAction
    provenance: AdministrationProvenance = field(
        default=AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
    )


@dataclass(frozen=True, slots=True)
class WebApiKeyOutcome:
    key_id: UUID | None
    secret: str | None = field(default=None, repr=False)


class WebApiKeyError(DomainError):
    """Safe refusal of a permissioned web API-key command."""


def _validate_web_actor(
    command: WebCreateApiKeyCommand | WebMutateApiKeyCommand, scope: str
) -> None:
    if (
        not isinstance(command, (WebCreateApiKeyCommand, WebMutateApiKeyCommand))
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or command.provenance
        is not AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR
        or command.actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or command.context.actor
        != f"{command.actor.actor_type.value}:{command.actor.actor_id}"
        or command.context.scope != scope
    ):
        raise WebApiKeyError(
            code=f"{OWNER}.invalid_actor",
            message="Trusted administrative actor required.",
        )


def _web_storage_error(exc: SQLAlchemyError) -> WebApiKeyError:
    busy = isinstance(exc, OperationalError) and (
        getattr(exc.orig, "sqlstate", None) == "55P03"
        or getattr(exc.orig, "pgcode", None) == "55P03"
    )
    return WebApiKeyError(
        code=f"{OWNER}.{'busy' if busy else 'storage_unavailable'}",
        message="API-key administration busy."
        if busy
        else "API-key administration unavailable.",
    )


def _web_timeout(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))


def _web_cap(db: Session, name: str) -> int:
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    value = settings_spec.resolve_value(db, SettingDomain.auth, name)
    try:
        return max(int(value), 0) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _web_audit(
    db: Session,
    *,
    command: WebCreateApiKeyCommand | WebMutateApiKeyCommand,
    key: ApiKey,
    action: str,
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type="api_key",
        entity_id=str(key.id),
        actor=AuditActor(command.actor.actor_type, command.actor.actor_id),
        metadata={
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        },
        details={"is_active": key.is_active},
    )


def web_create(db: Session, *, command: WebCreateApiKeyCommand) -> WebApiKeyOutcome:
    """Preserve best-effort web cap checks; concurrent writers can exceed caps."""
    _validate_web_actor(command, WEB_CREATE_SCOPE)
    if (
        not isinstance(command, WebCreateApiKeyCommand)
        or (
            command.subscriber_id is not None
            and not isinstance(command.subscriber_id, UUID)
        )
        or not isinstance(command.label, str)
        or not isinstance(command.scopes, tuple)
        or any(not isinstance(scope, str) for scope in command.scopes)
        or (
            command.expires_in_days is not None
            and type(command.expires_in_days) is not int
        )
    ):
        raise WebApiKeyError(
            code=f"{OWNER}.invalid_command", message="Invalid API-key creation command."
        )

    def operation() -> WebApiKeyOutcome:
        from datetime import timedelta

        from sqlalchemy import func

        from app.services.auth import hash_api_key

        _web_timeout(db)
        if (
            command.subscriber_id is not None
            and db.get(Subscriber, command.subscriber_id) is None
        ):
            raise WebApiKeyError(
                code=f"{OWNER}.subscriber_not_found", message="Subscriber not found"
            )
        max_per_owner = _web_cap(db, "api_key_max_per_owner")
        if max_per_owner > 0:
            active = int(
                db.scalar(
                    select(func.count())
                    .select_from(ApiKey)
                    .where(
                        ApiKey.is_active.is_(True),
                        ApiKey.revoked_at.is_(None),
                        ApiKey.subscriber_id == command.subscriber_id,
                    )
                )
                or 0
            )
            if active >= max_per_owner:
                raise WebApiKeyError(
                    code=f"{OWNER}.limit_reached",
                    message=f"Key limit reached: this owner already has {active} active key(s) (max {max_per_owner}). Revoke one before creating another.",
                )
        days = command.expires_in_days
        max_ttl = _web_cap(db, "api_key_max_ttl_days")
        if max_ttl > 0:
            if days is not None and days > max_ttl:
                raise WebApiKeyError(
                    code=f"{OWNER}.ttl_exceeded",
                    message=f"Expiration exceeds the {max_ttl}-day maximum allowed by policy. Choose a shorter lifetime.",
                )
            if days is None:
                days = max_ttl
        expires_at = (
            datetime.now(UTC) + timedelta(days=days) if days is not None else None
        )
        secret = secrets.token_urlsafe(32)
        key = ApiKey(
            subscriber_id=command.subscriber_id,
            label=command.label,
            scopes=list(command.scopes),
            key_hash=hash_api_key(secret),
            is_active=True,
            expires_at=expires_at,
        )
        db.add(key)
        db.flush()
        _web_audit(db, command=command, key=key, action="auth.api_key.created")
        return WebApiKeyOutcome(key.id, secret)

    try:
        return execute_owner_command(
            db,
            definition=_WEB_CREATE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except SQLAlchemyError as exc:
        raise _web_storage_error(exc) from None


def web_mutate(db: Session, *, command: WebMutateApiKeyCommand) -> WebApiKeyOutcome:
    """Recheck scope and active state under the exact target row lock."""
    if not isinstance(command, WebMutateApiKeyCommand) or not isinstance(
        command.action, WebApiKeyAction
    ):
        raise WebApiKeyError(
            code=f"{OWNER}.invalid_command", message="Invalid API-key mutation command."
        )
    _validate_web_actor(
        command,
        WEB_REVOKE_SCOPE
        if command.action is WebApiKeyAction.REVOKE
        else WEB_ROTATE_SCOPE,
    )
    if (
        not isinstance(command.key_id, UUID)
        or not isinstance(command.caller_scope, WebApiKeyCallerScope)
        or (
            command.caller_scope.subscriber_id is not None
            and not isinstance(command.caller_scope.subscriber_id, UUID)
        )
        or type(command.caller_scope.invalid_subscriber_id) is not bool
    ):
        raise WebApiKeyError(
            code=f"{OWNER}.invalid_command", message="Invalid API-key mutation command."
        )

    def operation() -> WebApiKeyOutcome:
        from app.services.auth import hash_api_key

        _web_timeout(db)
        key = db.scalar(
            select(ApiKey)
            .where(ApiKey.id == command.key_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if key is None:
            return WebApiKeyOutcome(None)
        if key.subscriber_id is not None:
            if command.caller_scope.invalid_subscriber_id:
                raise WebApiKeyError(
                    code=f"{OWNER}.invalid_scope", message="Invalid subscriber scope."
                )
            if key.subscriber_id != command.caller_scope.subscriber_id:
                return WebApiKeyOutcome(None)
        secret = None
        if command.action is WebApiKeyAction.ROTATE:
            if key.revoked_at is not None or not key.is_active:
                return WebApiKeyOutcome(None)
            secret = secrets.token_urlsafe(32)
            key.key_hash = hash_api_key(secret)
            action = "auth.api_key.rotated"
        else:
            key.is_active = False
            key.revoked_at = datetime.now(UTC)
            action = "auth.api_key.revoked"
        db.flush()
        _web_audit(db, command=command, key=key, action=action)
        return WebApiKeyOutcome(key.id, secret)

    try:
        return execute_owner_command(
            db,
            definition=_WEB_MUTATE_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except SQLAlchemyError as exc:
        raise _web_storage_error(exc) from None


@dataclass(frozen=True, slots=True)
class StaffKeyDeletionCommand:
    context: CommandContext = field(repr=False)
    actor: AuditActor = field(repr=False)
    system_user_ids: tuple[UUID, ...]
    provenance: AdministrationProvenance = field(
        default=AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR, init=False
    )


@dataclass(frozen=True, slots=True)
class StaffKeyDeletionOutcome:
    deleted_key_ids: tuple[UUID, ...]


def stage_staff_key_deletion(
    db: Session, *, command: StaffKeyDeletionCommand
) -> StaffKeyDeletionOutcome:
    """Named flush-only participant of inactive staff deletion, never a public command."""
    if (
        not isinstance(command, StaffKeyDeletionCommand)
        or command.provenance
        is not AdministrationProvenance.AUTHENTICATED_ADMINISTRATOR
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or not isinstance(command.system_user_ids, tuple)
        or any(not isinstance(value, UUID) for value in command.system_user_ids)
        or not owner_command_active(db, owner="auth.staff_provisioning")
        or current_command_definition(db).concern != "inactive staff account deletion"
        or current_command_definition(db).name
        not in {"delete_staff_account", "bulk_delete_staff_accounts"}
        or current_command_context(db) != command.context
        or command.context.scope != "rbac:assign"
        or command.actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or command.context.actor
        != f"{command.actor.actor_type.value}:{command.actor.actor_id}"
    ):
        raise DomainError(
            code=f"{OWNER}.invalid_participant",
            message="Trusted staff deletion participant context required.",
        )
    key_ids = tuple(
        db.scalars(
            select(ApiKey.id)
            .where(ApiKey.system_user_id.in_(command.system_user_ids))
            .order_by(ApiKey.id)
            .with_for_update()
        )
    )
    actual = (
        tuple(
            sorted(
                db.scalars(
                    delete(ApiKey).where(ApiKey.id.in_(key_ids)).returning(ApiKey.id)
                ),
                key=str,
            )
        )
        if key_ids
        else ()
    )
    for key_id in actual:
        stage_audit_event(
            db,
            action="auth.api_key.deleted",
            entity_type="api_key",
            entity_id=str(key_id),
            actor=AuditActor(command.actor.actor_type, command.actor.actor_id),
            details={
                "schema_version": 1,
                "scope": "rbac:assign",
                "reason": "inactive_staff_account_deletion",
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
            },
        )
    db.flush()
    return StaffKeyDeletionOutcome(actual)


AUTHENTICATION_CONCERN = "local API-key authentication maintenance"
_AUTHENTICATION_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=AUTHENTICATION_CONCERN, name="authenticate"
)
_AUTHENTICATION_REFRESH = timedelta(minutes=5)


class ApiKeyAuthenticationAudience(StrEnum):
    GENERAL = "general"
    AUDIT = "audit"


class ApiKeyAuthenticationProvenance(StrEnum):
    PRESENTED_API_KEY = "presented_api_key"


@dataclass(frozen=True, slots=True)
class AuthenticateApiKeyCommand:
    raw_key: str = field(repr=False)
    audience: ApiKeyAuthenticationAudience
    provenance: ApiKeyAuthenticationProvenance = field(
        default=ApiKeyAuthenticationProvenance.PRESENTED_API_KEY, init=False
    )


@dataclass(frozen=True, slots=True)
class AuthenticatedApiKey:
    key_id: UUID
    subscriber_id: UUID | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApiKeyAuthenticationOutcome:
    principal: AuthenticatedApiKey | None


@dataclass(frozen=True, slots=True)
class _AuthenticationRow:
    key_id: UUID
    key_hash: str = field(repr=False)
    is_active: bool
    revoked_at: datetime | None
    expires_at: datetime | None
    subscriber_id: UUID | None
    scopes: tuple[str, ...]
    last_used_at: datetime | None


class ApiKeyAuthenticationError(DomainError):
    """Sanitized authentication storage or command refusal."""


class _AuthenticationDenied(Exception):
    """Rollback any staged maintenance before returning a denied outcome."""


_AUTHENTICATION_COLUMNS = (
    ApiKey.id,
    ApiKey.key_hash,
    ApiKey.is_active,
    ApiKey.revoked_at,
    ApiKey.expires_at,
    ApiKey.subscriber_id,
    ApiKey.scopes,
    ApiKey.last_used_at,
)


def _authentication_row(
    row: Row[
        tuple[
            UUID,
            str,
            bool,
            datetime | None,
            datetime | None,
            UUID | None,
            list[str],
            datetime | None,
        ]
    ],
) -> _AuthenticationRow:
    return _AuthenticationRow(
        key_id=cast(UUID, row[0]),
        key_hash=cast(str, row[1]),
        is_active=cast(bool, row[2]),
        revoked_at=cast(datetime | None, row[3]),
        expires_at=cast(datetime | None, row[4]),
        subscriber_id=cast(UUID | None, row[5]),
        scopes=tuple(cast(list[str] | None, row[6]) or ()),
        last_used_at=cast(datetime | None, row[7]),
    )


def _authentication_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _authentication_allowed(
    row: _AuthenticationRow, audience: ApiKeyAuthenticationAudience
) -> bool:
    return (
        row.is_active
        and row.revoked_at is None
        and (
            row.expires_at is None
            or _authentication_utc(row.expires_at) > datetime.now(UTC)
        )
        and (
            audience is ApiKeyAuthenticationAudience.GENERAL
            or bool({"audit:read", "audit:*"}.intersection(row.scopes))
        )
    )


def authenticate(
    db: Session, *, command: AuthenticateApiKeyCommand
) -> ApiKeyAuthenticationOutcome:
    """Verify a presented local key and conditionally maintain its exact version.

    Read projections never load an ORM credential. The single CAS arbitrates
    rotation/revocation; a miss permits one fresh same-ID verification read.
    """
    from app.services.auth import hash_api_key_candidates, is_legacy_api_key_hash

    if (
        not isinstance(command, AuthenticateApiKeyCommand)
        or not isinstance(command.raw_key, str)
        or not command.raw_key
        or not isinstance(command.audience, ApiKeyAuthenticationAudience)
        or command.provenance is not ApiKeyAuthenticationProvenance.PRESENTED_API_KEY
    ):
        raise ApiKeyAuthenticationError(
            code=f"{OWNER}.invalid_authentication_command",
            message="Invalid API-key authentication command.",
        )
    context = CommandContext.system(
        actor="system:api_key_authentication",
        scope="api_key:authenticate",
        reason="local_api_key_authentication_maintenance",
    )

    def operation() -> ApiKeyAuthenticationOutcome:
        candidates = tuple(hash_api_key_candidates(command.raw_key))
        preferred = candidates[0]
        now = datetime.now(UTC)
        valid = (
            ApiKey.is_active.is_(True),
            ApiKey.revoked_at.is_(None),
            or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
        observed = db.execute(
            select(*_AUTHENTICATION_COLUMNS)
            .where(ApiKey.key_hash.in_(candidates), *valid)
            .limit(1)
        ).first()
        if observed is None:
            return ApiKeyAuthenticationOutcome(None)
        row = _authentication_row(observed)
        if not _authentication_allowed(row, command.audience):
            return ApiKeyAuthenticationOutcome(None)
        upgrade = is_legacy_api_key_hash(row.key_hash) and not is_legacy_api_key_hash(
            preferred
        )
        due = row.last_used_at is None or (
            now - _authentication_utc(row.last_used_at) >= _AUTHENTICATION_REFRESH
        )
        if not upgrade and not due:
            return ApiKeyAuthenticationOutcome(
                AuthenticatedApiKey(row.key_id, row.subscriber_id, row.scopes)
            )
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
        # The timestamp CASE is evaluated against CURRENT row values after a
        # concurrent updater releases its lock. Never regress a newer touch.
        due_predicate = or_(
            ApiKey.last_used_at.is_(None),
            ApiKey.last_used_at <= now - _AUTHENTICATION_REFRESH,
        )
        statement = (
            sql_update(ApiKey)
            .where(
                ApiKey.id == row.key_id,
                ApiKey.key_hash == row.key_hash,
                ApiKey.is_active.is_(True),
                ApiKey.revoked_at.is_(None),
                or_(
                    ApiKey.expires_at.is_(None),
                    ApiKey.expires_at
                    > (
                        func.clock_timestamp()
                        if db.get_bind().dialect.name == "postgresql"
                        else now
                    ),
                ),
                or_(true() if upgrade else false(), due_predicate),
            )
            .values(last_used_at=case((due_predicate, now), else_=ApiKey.last_used_at))
            .returning(*_AUTHENTICATION_COLUMNS)
            .execution_options(synchronize_session=False)
        )
        if upgrade:
            statement = statement.values(key_hash=preferred)
        changed = db.execute(statement).first()
        if changed is None:
            # Exactly one fresh scalar read; a rotated/revoked/deleted key must
            # never reuse the pre-CAS snapshot. Equivalent upgrade is accepted.
            fresh = db.execute(
                select(*_AUTHENTICATION_COLUMNS).where(
                    ApiKey.id == row.key_id,
                    ApiKey.key_hash.in_(candidates),
                    ApiKey.is_active.is_(True),
                    ApiKey.revoked_at.is_(None),
                    or_(
                        ApiKey.expires_at.is_(None),
                        ApiKey.expires_at > datetime.now(UTC),
                    ),
                )
            ).first()
            if fresh is None:
                return ApiKeyAuthenticationOutcome(None)
            row = _authentication_row(fresh)
        else:
            row = _authentication_row(changed)
        if not _authentication_allowed(row, command.audience):
            # Returning None here would COMMIT a maintenance write for an
            # expired or no-longer-audit-authorized principal.
            raise _AuthenticationDenied
        if changed is not None and upgrade:
            stage_audit_event(
                db,
                action="auth.api_key.hash_upgraded",
                entity_type="api_key",
                entity_id=str(row.key_id),
                actor=AuditActor.api_key(str(row.key_id)),
                details={
                    "schema_version": 1,
                    "old_scheme": "sha256",
                    "new_scheme": "hmac256",
                    "scope": "api_key:authenticate",
                    "reason": "local_api_key_authentication_maintenance",
                    "command_id": str(context.command_id),
                    "correlation_id": str(context.correlation_id),
                },
            )
        if not _authentication_allowed(row, command.audience):
            raise _AuthenticationDenied
        return ApiKeyAuthenticationOutcome(
            AuthenticatedApiKey(row.key_id, row.subscriber_id, row.scopes)
        )

    try:
        return execute_owner_command(
            db,
            definition=_AUTHENTICATION_DEFINITION,
            context=context,
            operation=operation,
        )
    except _AuthenticationDenied:
        return ApiKeyAuthenticationOutcome(None)
    except SQLAlchemyError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) if hasattr(exc, "orig") else None
        raise ApiKeyAuthenticationError(
            code=f"{OWNER}.authentication_busy"
            if sqlstate == "55P03"
            else f"{OWNER}.authentication_unavailable",
            message="API-key authentication unavailable.",
        ) from None
