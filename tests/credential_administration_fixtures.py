"""Typed command fixtures for administrative credential unit consumers."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas.auth import UserCredentialCreate, UserCredentialUpdate
from app.services import credential_administration as owner
from app.services.owner_commands import CommandContext


def context() -> CommandContext:
    return CommandContext.system(
        actor="service:credential-unit-test",
        scope=owner.SCOPE,
        reason="verify credential service consumer",
    )


def create(db: Session, payload: UserCredentialCreate) -> owner.CredentialView:
    # The legacy test fixtures explicitly finish their setup/read transaction.
    db.commit()
    return owner.create_credential(
        db,
        command=owner.CreateCredential(
            context(),
            payload.subscriber_id,
            payload.username,
            payload.provider,
            payload.radius_server_id,
            payload.password,
        ),
    ).credential


def update(
    db: Session, credential_id: str, payload: UserCredentialUpdate
) -> owner.CredentialView:
    db.commit()
    return owner.update_credential(
        db,
        command=owner.UpdateCredential(
            context(),
            UUID(credential_id),
            frozenset(owner.CredentialField(name) for name in payload.model_fields_set),
            payload.username,
            payload.radius_server_id,
            payload.password,
        ),
    ).credential


def deactivate(db: Session, credential_id: str) -> owner.CredentialView:
    db.commit()
    return owner.deactivate_credential(
        db, command=owner.DeactivateCredential(context(), UUID(credential_id))
    ).credential
