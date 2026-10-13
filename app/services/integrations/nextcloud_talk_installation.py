"""Installation-owned credential setup for an existing Nextcloud Talk binding."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.integration_platform import (
    IntegrationConfigRevision,
    IntegrationInstallationState,
)
from app.schemas.audit import AuditEventCreate
from app.services.audit import audit_events
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.integrations import installations
from app.services.integrations.connectors.nextcloud_talk import (
    NEXTCLOUD_TALK_CAPABILITY,
)
from app.services.integrations.nextcloud_talk_capability import (
    validate_installation_connection,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.secrets import (
    CreateTalkCredential,
    ImmutableSecretError,
    create_talk_credential,
    talk_credential_reference,
)

TALK_CREDENTIAL_SCOPE = "integration-installation:configure-nextcloud-talk-credential"
_DEFINITION = OwnerCommandDefinition(
    owner="integration.installations",
    concern="Nextcloud Talk installation credential configuration",
    name="configure_nextcloud_talk_credential",
)


class TalkCredentialError(DomainError, ValueError):
    """Safe, stable installation rejection."""


def _error(code: str) -> TalkCredentialError:
    return TalkCredentialError(
        code=f"integration.installations.talk_credential_{code}",
        message="This setup request is no longer valid. Reload the page and try again.",
    )


@dataclass(frozen=True, slots=True)
class ConfigureTalkCredential:
    context: CommandContext
    installation_id: UUID
    expected_revision_id: UUID
    expected_pin: installations.ManifestPin
    operation_id: UUID
    username: str
    app_password: SecretStr = field(repr=False)


class TalkCredentialStatus(StrEnum):
    enabled = "enabled"
    validation_failed = "validation_failed"
    storage_unavailable = "storage_unavailable"


@dataclass(frozen=True, slots=True)
class TalkCredentialOutcome:
    installation_id: UUID
    revision_id: UUID
    status: TalkCredentialStatus
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class TalkCredentialProjection:
    installation_id: UUID
    revision_id: UUID
    pin: installations.ManifestPin
    base_url: str
    username: str
    credential_bound: bool
    state: IntegrationInstallationState


def get_talk_credential_projection(
    db: Session, *, installation_id: UUID
) -> TalkCredentialProjection:
    installation = installations.get_installation(db, installation_id)
    revision = installation.current_config_revision
    if installation.connector_key != "nextcloud.talk" or revision is None:
        raise _error("invalid")
    return TalkCredentialProjection(
        installation_id=installation.id,
        revision_id=revision.id,
        pin=installations.ManifestPin(
            installation.connector_version, installation.manifest_digest
        ),
        base_url=str(revision.config_json.get("url") or ""),
        username=str(revision.config_json.get("notifier_username") or ""),
        credential_bound=bool(revision.secret_refs.get("app_password")),
        state=IntegrationInstallationState(installation.state),
    )


def configure_talk_credential(
    db: Session, command: ConfigureTalkCredential
) -> TalkCredentialOutcome:
    return execute_owner_command(
        db,
        definition=_DEFINITION,
        context=command.context,
        operation=lambda: _configure(db, command=command, context=command.context),
    )


def _evidence(
    db: Session,
    *,
    command: ConfigureTalkCredential,
    context: CommandContext,
    revision_id: UUID,
    status: TalkCredentialStatus,
) -> TalkCredentialOutcome:
    evidence = {
        "schema_version": 1,
        "installation_id": str(command.installation_id),
        "config_revision_id": str(revision_id),
        "operation_id": str(command.operation_id),
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "status": status,
    }
    audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user
            if context.actor.startswith("user:")
            else AuditActorType.service,
            actor_id=context.actor.removeprefix("user:"),
            action="integration.nextcloud_talk.credentials.configure",
            entity_type="integration_installation",
            entity_id=str(command.installation_id),
            is_success=status == "enabled",
            details=evidence,
        ),
    )
    emit_event(
        db,
        EventType.integration_installation_talk_credential_configured,
        evidence,
        actor=context.actor,
    )
    return TalkCredentialOutcome(command.installation_id, revision_id, status)


def _configure(
    db: Session, *, command: ConfigureTalkCredential, context: CommandContext
) -> TalkCredentialOutcome:
    if context.scope != TALK_CREDENTIAL_SCOPE:
        raise _error("scope_invalid")
    if (
        not command.username.strip()
        or len(command.username) > 160
        or not command.app_password.get_secret_value()
        or len(command.app_password.get_secret_value()) > 4096
    ):
        raise _error("invalid")
    installation = installations.lock_installation(db, command.installation_id)
    revision = installation.current_config_revision
    if (
        installation.connector_key != "nextcloud.talk"
        or installation.state in {"retired", "quarantined"}
        or revision is None
        or not revision.config_json.get("url")
    ):
        raise _error("invalid")
    pin = installations.ManifestPin(
        installation.connector_version, installation.manifest_digest
    )
    if pin != command.expected_pin:
        raise _error("stale")
    installations._definition_for_installation(installation)
    bindings = installation.capability_bindings
    if len(bindings) != 1 or bindings[0].capability_id != NEXTCLOUD_TALK_CAPABILITY:
        raise _error("binding_invalid")
    prior = db.scalar(
        select(IntegrationConfigRevision).where(
            IntegrationConfigRevision.id == command.expected_revision_id,
            IntegrationConfigRevision.installation_id == installation.id,
        )
    )
    if prior is None:
        raise _error("stale")
    config = dict(prior.config_json)
    config["notifier_username"] = command.username.strip()
    fingerprint = installations.config_revision_digest(
        config={
            "config": config,
            "installation_id": str(installation.id),
            "actor": context.actor,
            "scope": context.scope,
            "expected_revision_id": str(command.expected_revision_id),
            "binding_scope": dict(bindings[0].scope_json or {}),
            "binding_policy": dict(bindings[0].policy_json or {}),
            "manifest_version": pin.connector_version,
            "manifest_digest": pin.manifest_digest,
        },
        secret_refs=dict(prior.secret_refs),
        schema_version=prior.schema_version,
    )
    secret_command = CreateTalkCredential(
        installation.id,
        command.operation_id,
        installation.environment,
        command.app_password,
        fingerprint,
    )
    reference = talk_credential_reference(secret_command)
    used = list(
        db.scalars(
            select(IntegrationConfigRevision).where(
                IntegrationConfigRevision.installation_id == installation.id
            )
        ).all()
    )
    operation_revisions = [
        row for row in used if row.secret_refs.get("app_password") == reference
    ]
    replay = bool(operation_revisions)
    if replay:
        if revision not in operation_revisions:
            raise _error("stale")
        if dict(revision.config_json) != config:
            raise _error("conflict")
    elif revision.id != command.expected_revision_id:
        raise _error("stale")
    try:
        created = create_talk_credential(secret_command)
    except ImmutableSecretError as exc:
        if exc.code.endswith(".conflict"):
            raise _error("conflict") from None
        return _evidence(
            db,
            command=command,
            context=context,
            revision_id=revision.id,
            status=TalkCredentialStatus.storage_unavailable,
        )
    if replay:
        return TalkCredentialOutcome(
            installation.id,
            revision.id,
            TalkCredentialStatus.enabled
            if installation.state == "enabled"
            else TalkCredentialStatus.validation_failed,
            True,
        )
    refs = dict(revision.secret_refs)
    refs["app_password"] = created.reference
    candidate = installations.create_config_revision(
        db,
        installation_id=installation.id,
        config=config,
        secret_refs=refs,
        schema_version=revision.schema_version,
        actor=context.actor,
    )
    installations.disable_installation(
        db,
        installation_id=installation.id,
        reason="credential_validation_required",
        actor=context.actor,
    )
    static = installations.validate_static(
        db, installation_id=installation.id, actor=context.actor
    )
    valid = False
    if static.valid:
        try:
            valid = validate_installation_connection(
                db, capability_binding_id=bindings[0].id
            ).valid
        except Exception:
            # Provider/resolver exception details can contain material; retain only status.
            valid = False
    if valid:
        from app.services.integrations.runtime import ValidationResult

        installations.enable_after_connection_validation(
            db,
            installation_id=installation.id,
            connection_result=ValidationResult(valid=True),
            actor=context.actor,
        )
    else:
        installations.disable_installation(
            db,
            installation_id=installation.id,
            reason="credential_validation_failed",
            actor=context.actor,
        )
    return _evidence(
        db,
        command=command,
        context=context,
        revision_id=candidate.id,
        status=TalkCredentialStatus.enabled
        if valid
        else TalkCredentialStatus.validation_failed,
    )
