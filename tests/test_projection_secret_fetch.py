"""Non-authoritative SQLite unit lane; PostgreSQL owns concurrency evidence."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential
from app.models.event_store import EventStore
from app.models.projection_secret_request import ProjectionSecretRequest
from app.services.credential_crypto import encrypt_credential
from app.services.integrations import projection_secret_fetch as owner
from app.services.owner_commands import CommandContext, OwnerCommandError

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _jsonb_as_json(type_: object, compiler: object, **kw: object) -> str:
        return "JSON"

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_engine("sqlite://")
    AccessCredential.__table__.create(engine)
    ProjectionSecretRequest.__table__.create(engine)
    EventStore.__table__.create(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        engine.dispose()


def _prepare(db: Session) -> owner.RedeemProjectionSecretCommand:
    credential = AccessCredential(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        username="dummy@radius",
        secret_hash=encrypt_credential("dummy-projection-secret"),
        is_active=True,
        updated_at=NOW,
    )
    db.add(credential)
    db.flush()
    request = owner.open_projection_request(db, credential=credential, now=NOW)
    command = owner.RedeemProjectionSecretCommand(
        context=CommandContext.system(
            actor="test:projection-secret",
            scope=owner.PROJECTION_SECRET_FETCH_SCOPE,
            reason="verify single-use projection fetch",
        ),
        request_ref=request.request_ref,
        credential_id=credential.id,
        secret_version=request.secret_version,
        now=NOW + timedelta(minutes=1),
    )
    db.commit()
    return command


def test_open_then_redeem_returns_secret_exactly_once(db: Session) -> None:
    command = _prepare(db)
    result = owner.redeem_projection_secret(db, command=command)
    assert result.secret == "dummy-projection-secret"
    assert result.secret not in repr(result)
    assert not db.in_transaction()
    with pytest.raises(
        owner.SecretFetchRefused, match="^projection secret fetch refused$"
    ):
        owner.redeem_projection_secret(db, command=command)
    assert not db.in_transaction()
    assert len(db.scalars(select(EventStore)).all()) == 1


@pytest.mark.parametrize(
    "change", ["rotation", "inactive", "reference", "missing_secret"]
)
def test_unusable_credential_refuses_without_burn(db: Session, change: str) -> None:
    command = _prepare(db)
    credential = db.get(AccessCredential, command.credential_id)
    assert credential is not None
    if change == "rotation":
        credential.updated_at = NOW + timedelta(minutes=3)
    elif change == "inactive":
        credential.is_active = False
    elif change == "reference":
        credential.secret_hash = "openbao://dummy/secret"
    else:
        credential.secret_hash = None
    db.commit()
    with pytest.raises(owner.SecretFetchRefused):
        owner.redeem_projection_secret(db, command=command)
    request = db.scalar(select(ProjectionSecretRequest))
    assert request is not None and request.redeemed_at is None
    assert not db.scalars(select(EventStore)).all()


@pytest.mark.parametrize(
    "change", ["expired", "unknown", "wrong_credential", "wrong_version"]
)
def test_expired_and_mismatched_refs_refuse(db: Session, change: str) -> None:
    from dataclasses import replace

    command = _prepare(db)
    if change == "expired":
        command = replace(command, now=NOW + timedelta(hours=1))
    elif change == "unknown":
        command = replace(command, request_ref="projection-request:unknown")
    elif change == "wrong_credential":
        command = replace(command, credential_id=uuid.uuid4())
    else:
        command = replace(command, secret_version=command.secret_version + 1)
    with pytest.raises(owner.SecretFetchRefused) as error:
        owner.redeem_projection_secret(db, command=command)
    assert error.value.code == "access.projection_secret_fetch.refused"
    assert error.value.details == {}
    assert not db.in_transaction()


def test_active_caller_transaction_is_rejected(db: Session) -> None:
    command = _prepare(db)
    db.scalar(select(ProjectionSecretRequest))
    with pytest.raises(OwnerCommandError) as error:
        owner.redeem_projection_secret(db, command=command)
    assert error.value.code.endswith(".active_caller_transaction")
    assert not db.in_transaction()
