"""Password-only administrative credential command coordinator.

Password policy and hashing run in a separate read session before acquiring
principal -> credential locks. Caller transactions are always rejected, even
when all their writes have already been flushed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.auth import AuthProvider, UserCredential
from app.models.domain_settings import SettingDomain
from app.models.subscriber import ResellerUser, Subscriber, SubscriberCategory
from app.models.system_user import SystemUser
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value

OutcomeT = TypeVar("OutcomeT")

OWNER = "auth.credential_administration"
CONCERN = "administrative credential and subscriber login transition"
SCOPE = "auth:credential:write"
_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="administer_credential"
)


class CredentialAdministrationError(DomainError):
    """Safe command rejection, with no submitted secret in its evidence."""


def error(code: str, message: str) -> CredentialAdministrationError:
    return CredentialAdministrationError(
        code=f"{OWNER}.{code}", message=message, retryable=code == "lock_timeout"
    )


class PrincipalType(StrEnum):
    subscriber = "subscriber"
    system_user = "system_user"
    reseller_user = "reseller_user"


class CredentialField(StrEnum):
    username = "username"
    radius_server_id = "radius_server_id"
    password = "password"


@dataclass(frozen=True, slots=True)
class CreateCredential:
    context: CommandContext
    subscriber_id: UUID
    username: str | None
    provider: AuthProvider | None = None
    radius_server_id: UUID | None = None
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class UpdateCredential:
    context: CommandContext
    credential_id: UUID
    submitted_fields: frozenset[CredentialField]
    username: str | None = None
    radius_server_id: UUID | None = None
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DeactivateCredential:
    context: CommandContext
    credential_id: UUID


@dataclass(frozen=True, slots=True)
class ProvisionSubscriberLogin:
    context: CommandContext
    subscriber_id: UUID
    business: bool
    subscriber_number: str | None
    category: SubscriberCategory | None
    notes: str | None
    create_login: bool = False
    username: str | None = None
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CredentialView:
    id: UUID
    subscriber_id: UUID | None
    provider: AuthProvider
    username: str | None
    radius_server_id: UUID | None
    must_change_password: bool
    password_updated_at: datetime | None
    failed_login_attempts: int
    locked_until: datetime | None
    last_login_at: datetime | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: UserCredential) -> CredentialView:
        return cls(**{name: getattr(row, name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class CredentialOutcome:
    credential: CredentialView
    credential_version: int
    sessions_revoked: int
    changed: bool
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class SubscriberLoginOutcome:
    subscriber_id: UUID
    credential: CredentialOutcome | None
    command_id: UUID
    correlation_id: UUID


def _assert_boundary(db: Session, context: CommandContext) -> None:
    if db.in_transaction():
        raise error(
            "active_caller_transaction",
            "Credential administration requires a transaction-free session.",
        )
    if context.scope != SCOPE or not context.actor.strip():
        raise error(
            "invalid_command_context",
            "Credential administration requires explicit authorized actor evidence.",
        )


def _principal(row: UserCredential) -> tuple[PrincipalType, UUID]:
    for kind in PrincipalType:
        value = getattr(row, f"{kind.value}_id")
        if value is not None:
            return kind, value
    raise error("invalid_principal", "Credential principal is missing.")


def _lock_principal(db: Session, kind: PrincipalType, principal_id: UUID) -> None:
    models: dict[
        PrincipalType, type[Subscriber] | type[SystemUser] | type[ResellerUser]
    ] = {
        PrincipalType.subscriber: Subscriber,
        PrincipalType.system_user: SystemUser,
        PrincipalType.reseller_user: ResellerUser,
    }
    model = models[kind]
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
    if (
        db.scalar(
            select(model)
            .where(model.id == principal_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        is None
    ):
        raise error("principal_not_found", "Credential principal was not found.")


def _prepare_password(
    db: Session, kind: PrincipalType, password: str | None, required: bool
) -> str | None:
    if not required:
        if password is not None:
            raise error(
                "invalid_password", "Passwords are accepted only for local credentials."
            )
        return None
    if password is None:
        raise error("invalid_password", "Local credentials require a password.")
    if len(password) > 255:
        raise error(
            "invalid_password",
            "Password must contain at most 255 valid Unicode characters.",
        )
    try:
        password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise error(
            "invalid_password", "Password must contain valid Unicode characters."
        ) from exc
    from app.services.auth_flow import (
        hash_password,
        password_min_length_for,
        password_policy_violations,
    )

    violations = password_policy_violations(
        password or "", password_min_length_for(db, kind.value)
    )
    if violations:
        raise error("invalid_password", violations[0])
    return hash_password(password or "")


def _run(
    db: Session, context: CommandContext, operation: Callable[[], OutcomeT]
) -> OutcomeT:
    try:
        return execute_owner_command(
            db, definition=_DEFINITION, context=context, operation=operation
        )
    except IntegrityError as exc:
        raise error("conflict", "Credential identity is already in use.") from exc
    except OperationalError as exc:
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if state == "55P03":
            raise error("lock_timeout", "Credential is busy; retry shortly.") from exc
        raise


def create_credential(db: Session, *, command: CreateCredential) -> CredentialOutcome:
    _assert_boundary(db, command.context)
    with SessionLocal(
        bind=db.get_bind(), join_transaction_mode="create_savepoint"
    ) as preparation:
        provider = command.provider
        if provider is None:
            value = resolve_value(
                preparation, SettingDomain.auth, "default_auth_provider"
            )
            try:
                provider = AuthProvider(value) if value else AuthProvider.local
            except ValueError as exc:
                raise error(
                    "invalid_provider", "Configured credential provider is invalid."
                ) from exc
        hashed = _prepare_password(
            preparation,
            PrincipalType.subscriber,
            command.password,
            provider == AuthProvider.local,
        )

    def operation() -> CredentialOutcome:
        from app.services.credential_administration_mutation import create

        _lock_principal(db, PrincipalType.subscriber, command.subscriber_id)
        if provider == AuthProvider.local:
            existing = db.scalar(
                select(UserCredential.id).where(
                    UserCredential.subscriber_id == command.subscriber_id,
                    UserCredential.provider == AuthProvider.local,
                    UserCredential.is_active.is_(True),
                )
            )
            if existing:
                raise error("conflict", "Customer already has a portal login.")
        return create(db, command=command, provider=provider, password_hash=hashed)

    return _run(db, command.context, operation)


def update_credential(db: Session, *, command: UpdateCredential) -> CredentialOutcome:
    _assert_boundary(db, command.context)
    with SessionLocal(
        bind=db.get_bind(), join_transaction_mode="create_savepoint"
    ) as preparation:
        row = preparation.get(UserCredential, command.credential_id)
        if row is None:
            raise error("credential_not_found", "User credential was not found.")
        kind, principal_id = _principal(row)
        provider = row.provider
        if (
            CredentialField.password in command.submitted_fields
            and kind is not PrincipalType.subscriber
        ):
            raise error(
                "invalid_principal",
                "Staff and reseller passwords are managed by their own "
                "provisioning flows.",
            )
        if (
            CredentialField.password in command.submitted_fields
            and provider != AuthProvider.local
        ):
            raise error(
                "invalid_password", "Passwords are accepted only for local credentials."
            )
        hashed = (
            _prepare_password(
                preparation, kind, command.password, provider == AuthProvider.local
            )
            if CredentialField.password in command.submitted_fields
            else None
        )

    def operation() -> CredentialOutcome:
        from app.services.credential_administration_mutation import update

        _lock_principal(db, kind, principal_id)
        row = db.scalar(
            select(UserCredential)
            .where(UserCredential.id == command.credential_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or _principal(row) != (kind, principal_id)
            or row.provider != provider
        ):
            raise error(
                "stale_credential",
                "Credential identity changed; reload before retrying.",
            )
        return update(db, command=command, row=row, password_hash=hashed)

    return _run(db, command.context, operation)


def deactivate_credential(
    db: Session, *, command: DeactivateCredential
) -> CredentialOutcome:
    _assert_boundary(db, command.context)
    with SessionLocal(
        bind=db.get_bind(), join_transaction_mode="create_savepoint"
    ) as preparation:
        row = preparation.get(UserCredential, command.credential_id)
        if row is None:
            raise error("credential_not_found", "User credential was not found.")
        kind, principal_id = _principal(row)

    def operation() -> CredentialOutcome:
        from app.services.credential_administration_mutation import deactivate

        _lock_principal(db, kind, principal_id)
        row = db.scalar(
            select(UserCredential)
            .where(UserCredential.id == command.credential_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or _principal(row) != (kind, principal_id):
            raise error(
                "stale_credential",
                "Credential identity changed; reload before retrying.",
            )
        return deactivate(db, command=command, row=row)

    return _run(db, command.context, operation)


def provision_subscriber_login(
    db: Session, *, command: ProvisionSubscriberLogin
) -> SubscriberLoginOutcome:
    _assert_boundary(db, command.context)
    with SessionLocal(
        bind=db.get_bind(), join_transaction_mode="create_savepoint"
    ) as preparation:
        if command.create_login and command.business:
            raise error(
                "invalid_principal",
                "Customer portal logins require a person subscriber.",
            )
        hashed = (
            _prepare_password(
                preparation, PrincipalType.subscriber, command.password, True
            )
            if command.create_login
            else None
        )
        prefix_value = resolve_value(
            preparation, SettingDomain.subscriber, "subscriber_number_prefix"
        )
        prefix = prefix_value if isinstance(prefix_value, str) else "SUB-"

    def operation() -> SubscriberLoginOutcome:
        from app.services.credential_administration_mutation import create
        from app.services.customer_registration_metadata import (
            ApplyRegistrationMetadata,
            apply_registration_metadata,
        )

        _lock_principal(db, PrincipalType.subscriber, command.subscriber_id)
        if command.create_login:
            existing = db.scalar(
                select(UserCredential.id).where(
                    UserCredential.subscriber_id == command.subscriber_id,
                    UserCredential.provider == AuthProvider.local,
                    UserCredential.is_active.is_(True),
                )
            )
            if existing:
                raise error("conflict", "Customer already has a portal login.")
        apply_registration_metadata(
            db,
            command=ApplyRegistrationMetadata(
                command.context,
                command.subscriber_id,
                command.business,
                command.subscriber_number,
                command.category,
                command.notes,
                prefix,
            ),
        )
        result = (
            create(
                db,
                command=CreateCredential(
                    command.context,
                    command.subscriber_id,
                    command.username,
                    AuthProvider.local,
                ),
                provider=AuthProvider.local,
                password_hash=hashed,
            )
            if command.create_login
            else None
        )
        return SubscriberLoginOutcome(
            command.subscriber_id,
            result,
            command.context.command_id,
            command.context.correlation_id,
        )

    return _run(db, command.context, operation)
