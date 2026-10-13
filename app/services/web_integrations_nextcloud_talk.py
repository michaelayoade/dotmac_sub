"""Typed admin adapter for installed Nextcloud Talk credential setup."""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.services.form_contracts import FormConsequence, FormContract, register
from app.services.integrations import installations
from app.services.integrations.nextcloud_talk_installation import (
    ConfigureTalkCredential,
    TalkCredentialOutcome,
    TalkCredentialProjection,
    configure_talk_credential,
    get_talk_credential_projection,
)
from app.services.owner_commands import CommandContext

TALK_CREDENTIAL_FORM = register(
    FormContract(
        key="integrations.nextcloud_talk.credentials",
        title="Save and validate notifier credentials",
        entity="integration_installation",
        command_owner="integration.installations",
        consequences=(
            FormConsequence(
                "immutable",
                "Creates a new stored app password; existing passwords remain unchanged.",
            ),
            FormConsequence(
                "validation",
                "Disables this Talk integration until its credentials pass a connection check. No test notification is sent.",
            ),
            FormConsequence(
                "scope",
                "Keeps the approved server, notification policy, and global notification setting unchanged.",
            ),
        ),
    )
)


@dataclass(frozen=True, slots=True)
class TalkCredentialForm:
    revision_id: str
    connector_version: str
    manifest_digest: str
    operation_id: str
    username: str
    app_password: SecretStr = field(repr=False)


@dataclass(frozen=True, slots=True)
class TalkCredentialPage:
    installation: TalkCredentialProjection
    operation_id: UUID
    error: str | None = None
    notice: str | None = None


def build_page(
    db: Session,
    *,
    installation_id: UUID,
    operation_id: UUID | None = None,
    error: str | None = None,
    notice: str | None = None,
) -> TalkCredentialPage:
    return TalkCredentialPage(
        installation=get_talk_credential_projection(
            db, installation_id=installation_id
        ),
        operation_id=operation_id or uuid4(),
        error=error,
        notice=notice,
    )


def save_credentials(
    db: Session,
    *,
    installation_id: UUID,
    form: TalkCredentialForm,
    context: CommandContext,
) -> TalkCredentialOutcome:
    return configure_talk_credential(
        db,
        ConfigureTalkCredential(
            context=context,
            installation_id=installation_id,
            expected_revision_id=UUID(form.revision_id),
            expected_pin=installations.ManifestPin(
                form.connector_version, form.manifest_digest
            ),
            operation_id=UUID(form.operation_id),
            username=form.username,
            app_password=form.app_password,
        ),
    )
