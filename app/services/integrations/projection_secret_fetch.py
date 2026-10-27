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

REQUEST_TTL = timedelta(minutes=15)


class SecretFetchRefused(RuntimeError):
    """The one opaque refusal: unknown, stale, spent, or unusable."""


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
    request_ref: str,
    credential_id: uuid.UUID,
    secret_version: int,
    now: datetime | None = None,
) -> str:
    """Burn the request and return the cleartext, or refuse opaquely.

    This command owns its transaction (the API port delegates and never
    commits). The burn (`redeemed_at`) is committed before decryption is
    attempted: a caller that crashes after receiving the secret cannot
    redeem the same ref again, and a failed decrypt still spends the
    authorization rather than leaving it replayable. Pre-burn refusals
    roll back having written nothing.
    """
    moment = now or datetime.now(UTC)
    request = db.scalar(
        select(ProjectionSecretRequest).where(
            ProjectionSecretRequest.request_ref == request_ref
        )
    )
    if (
        request is None
        or request.redeemed_at is not None
        or request.credential_id != credential_id
        or request.secret_version != secret_version
        or moment > request.expires_at
    ):
        db.rollback()
        raise SecretFetchRefused("projection secret fetch refused")
    credential = db.get(AccessCredential, credential_id)
    if (
        credential is None
        or not credential.is_active
        or current_secret_version(credential) != secret_version
        or not credential.secret_hash
        or is_secret_reference_value(credential.secret_hash)
    ):
        db.rollback()
        raise SecretFetchRefused("projection secret fetch refused")
    request.redeemed_at = moment
    db.commit()
    try:
        secret = decrypt_credential_with_key(
            credential.secret_hash, get_encryption_key()
        )
    except Exception as exc:  # noqa: BLE001 - refusal must stay opaque
        raise SecretFetchRefused("projection secret fetch refused") from exc
    if not secret:
        raise SecretFetchRefused("projection secret fetch refused")
    return secret


__all__ = [
    "SecretFetchRefused",
    "current_secret_version",
    "open_projection_request",
    "redeem_projection_secret",
]
