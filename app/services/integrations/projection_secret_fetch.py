"""Credential-owner side of the ADR-0076 §7 single-use secret fetch.

The connector never receives a secret in a plan. When the owner authors a
plan it opens a `ProjectionSecretRequest` naming the credential and the
secret version it planned against; the connector redeems that ref exactly
once, over an authenticated channel, and the request burns in the same
transaction that releases the cleartext. All refusal reasons collapse to
one opaque `SecretFetchRefused` so the endpoint cannot be used to probe
credential state.

Bridge note: the incumbent `AccessCredential` has no native secret
version, so the version is the credential row's `updated_at` epoch second
captured at open time. Any later write to the credential row (a password
change above all) moves the version and refuses stale fetches, which is
exactly the rotation race the version exists to catch. When the suite's
access-credentials module lands it owns real versions and this derivation
goes away with the incumbent store.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential
from app.models.projection_secret_request import ProjectionSecretRequest
from app.services.credential_crypto import (
    decrypt_credential_with_key,
    get_encryption_key,
    is_secret_reference_value,
)
from app.services.domain_errors import DomainError
from app.services.events.dispatcher import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

REQUEST_TTL = timedelta(minutes=15)


PROJECTION_SECRET_FETCH_SCOPE = "integration:projection-secret:reveal"
_REDEEM = OwnerCommandDefinition(
    owner="access.projection_secret_fetch",
    concern="single-use projection secret fetch authorizations",
    name="redeem_projection_secret",
)


class SecretFetchRefused(DomainError):
    """The one opaque refusal: unknown, stale, spent, or unusable."""

    def __init__(self) -> None:
        super().__init__(
            code="access.projection_secret_fetch.refused",
            message="projection secret fetch refused",
            retryable=False,
        )


@dataclass(frozen=True)
class RedeemProjectionSecretCommand:
    context: CommandContext
    request_ref: str
    credential_id: uuid.UUID
    secret_version: int
    now: datetime | None = None


@dataclass(frozen=True)
class ProjectionSecretRedeemed:
    secret: str = field(repr=False)


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def current_secret_version(credential: AccessCredential) -> int:
    updated = credential.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return int(updated.timestamp())


def open_projection_request(
    db: Session,
    *,
    credential: AccessCredential,
    now: datetime | None = None,
) -> ProjectionSecretRequest:
    """Authorize exactly one future fetch of this credential's secret."""
    moment = now or datetime.now(UTC)
    request = ProjectionSecretRequest(
        request_ref=f"projection-request:{uuid.uuid4()}",
        credential_id=credential.id,
        secret_version=current_secret_version(credential),
        opened_at=moment,
        expires_at=moment + REQUEST_TTL,
    )
    db.add(request)
    db.flush()
    return request


def redeem_projection_secret(
    db: Session,
    *,
    command: RedeemProjectionSecretCommand,
) -> ProjectionSecretRedeemed:
    """Release once, only after the burn and reveal outbox event commit.

    The adapter supplies a transaction-free session. Lock the credential
    before the request, refreshing both even when the session retained an
    older identity. Holding the credential through commit serializes rotation
    with the release. A pre-commit failure rolls back the whole command and
    leaves an unspent request retryable. An uncertain database COMMIT result
    may durably burn the request and event without returning cleartext;
    retry then refuses if that commit succeeded.
    """
    return execute_owner_command(
        db,
        definition=_REDEEM,
        context=command.context,
        operation=lambda: _redeem_projection_secret(db, command=command),
    )


def _redeem_projection_secret(
    db: Session, *, command: RedeemProjectionSecretCommand
) -> ProjectionSecretRedeemed:
    if command.context.scope != PROJECTION_SECRET_FETCH_SCOPE:
        raise SecretFetchRefused()
    credential = db.scalar(
        select(AccessCredential)
        .where(AccessCredential.id == command.credential_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    request = db.scalar(
        select(ProjectionSecretRequest)
        .where(ProjectionSecretRequest.request_ref == command.request_ref)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    moment = _utc(command.now or datetime.now(UTC))
    if (
        request is None
        or request.redeemed_at is not None
        or request.credential_id != command.credential_id
        or request.secret_version != command.secret_version
        or moment > _utc(request.expires_at)
        or credential is None
        or not credential.is_active
        or current_secret_version(credential) != command.secret_version
        or not credential.secret_hash
        or is_secret_reference_value(credential.secret_hash)
    ):
        raise SecretFetchRefused()
    try:
        secret = decrypt_credential_with_key(
            credential.secret_hash, get_encryption_key()
        )
    except Exception:  # noqa: BLE001 - refusal must stay opaque
        raise SecretFetchRefused() from None
    if not secret:
        raise SecretFetchRefused()
    request.redeemed_at = moment
    emit_event(
        db,
        EventType.access_projection_secret_revealed,
        {
            "schema_version": 1,
            "credential_id": str(credential.id),
            "secret_version": command.secret_version,
            "request_ref": command.request_ref,
        },
        actor=command.context.actor,
        account_id=credential.subscriber_id,
        subscription_id=credential.subscription_id,
        dispatch_after_commit=False,
    )
    db.flush()
    return ProjectionSecretRedeemed(secret=secret)


__all__ = [
    "PROJECTION_SECRET_FETCH_SCOPE",
    "ProjectionSecretRedeemed",
    "RedeemProjectionSecretCommand",
    "SecretFetchRefused",
    "current_secret_version",
    "open_projection_request",
    "redeem_projection_secret",
]
