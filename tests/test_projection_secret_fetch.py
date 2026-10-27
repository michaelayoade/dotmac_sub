"""Single-use projection secret fetch against the incumbent credential store."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.models.catalog import AccessCredential  # noqa: E402
from app.models.event_store import EventStore  # noqa: E402
from app.models.projection_secret_request import ProjectionSecretRequest  # noqa: E402
from app.services.credential_crypto import encrypt_credential  # noqa: E402
from app.services.integrations.projection_secret_fetch import (  # noqa: E402
    SecretFetchRefused,
    current_secret_version,
    open_projection_request,
    redeem_projection_secret,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _session() -> Session:
    # The shared conftest patches JSONB rendering for SQLite; this file's
    # two-table fixture needs the same shim for the event store table.
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb_as_json(type_: object, compiler: object, **kw: object) -> str:
        return "JSON"

    engine = create_engine("sqlite://")
    AccessCredential.__table__.create(engine)
    ProjectionSecretRequest.__table__.create(engine)
    EventStore.__table__.create(engine)
    return sessionmaker(bind=engine)()


def _credential(db: Session) -> AccessCredential:
    credential = AccessCredential(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        username="jane@radius",
        secret_hash=encrypt_credential("pw-jane"),
        is_active=True,
        updated_at=NOW,
    )
    db.add(credential)
    db.flush()
    return credential


def test_open_then_redeem_returns_secret_exactly_once() -> None:
    db = _session()
    credential = _credential(db)
    request = open_projection_request(db, credential=credential, now=NOW)
    secret = redeem_projection_secret(
        db,
        request_ref=request.request_ref,
        credential_id=credential.id,
        secret_version=current_secret_version(credential),
        now=NOW + timedelta(minutes=1),
    )
    assert secret == "pw-jane"
    with pytest.raises(SecretFetchRefused):
        redeem_projection_secret(
            db,
            request_ref=request.request_ref,
            credential_id=credential.id,
            secret_version=current_secret_version(credential),
            now=NOW + timedelta(minutes=2),
        )


def test_rotation_between_open_and_redeem_refuses() -> None:
    db = _session()
    credential = _credential(db)
    request = open_projection_request(db, credential=credential, now=NOW)
    credential.updated_at = NOW + timedelta(minutes=3)  # password rotated
    db.flush()
    with pytest.raises(SecretFetchRefused):
        redeem_projection_secret(
            db,
            request_ref=request.request_ref,
            credential_id=credential.id,
            secret_version=request.secret_version,
            now=NOW + timedelta(minutes=4),
        )


def test_expired_and_mismatched_refs_refuse() -> None:
    db = _session()
    credential = _credential(db)
    request = open_projection_request(db, credential=credential, now=NOW)
    with pytest.raises(SecretFetchRefused):
        redeem_projection_secret(
            db,
            request_ref=request.request_ref,
            credential_id=credential.id,
            secret_version=request.secret_version,
            now=NOW + timedelta(hours=1),
        )
    with pytest.raises(SecretFetchRefused):
        redeem_projection_secret(
            db,
            request_ref="projection-request:unknown",
            credential_id=credential.id,
            secret_version=request.secret_version,
            now=NOW,
        )
    with pytest.raises(SecretFetchRefused):
        redeem_projection_secret(
            db,
            request_ref=request.request_ref,
            credential_id=uuid.uuid4(),
            secret_version=request.secret_version,
            now=NOW,
        )
