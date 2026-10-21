"""Local collection only; hosted real-Alembic PostgreSQL proves these contracts."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import String, cast, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.schemas.auth import ApiKeyUpdate
from app.services import api_key_administration as owner
from tests.api_key_update_fixtures import update_command
from tests.integration import test_api_key_revocation_concurrency as revocations
from tests.integration import test_oidc_atomic_session_concurrency as races
from tests.integration.test_oidc_atomic_session_concurrency import Worker, _blocked_by

engine = races.engine
key_id = revocations.key_id


def update_worker(
    engine: Engine, target: UUID, worker: Worker, pause: bool = False
) -> owner.ApiKeyUpdateOutcome:
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        return owner.update(
            db,
            command=update_command(target, ApiKeyUpdate(label="inert-updated-label")),
        )


def snapshot(engine: Engine, target: UUID) -> tuple[bool, str, tuple[str, ...]]:
    with Session(engine) as db:
        row = db.get(ApiKey, target)
        assert row is not None
        actions = tuple(
            db.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.entity_id == str(target))
                .order_by(AuditEvent.occurred_at)
            )
        )
        return row.is_active, row.key_hash, actions


def test_update_then_revoke_serialize_on_exact_key(
    engine: Engine, key_id: UUID
) -> None:
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(update_worker, engine, key_id, blocker, True)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            second = pool.submit(revocations.revoke_worker, engine, key_id, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        assert first.result(timeout=15).label == "inert-updated-label"
        second.result(timeout=15)
    active, _, actions = snapshot(engine, key_id)
    assert not active
    assert actions == ("auth.api_key.updated", "auth.api_key.revoked")


def test_update_lock_timeout_sanitized_and_rolled_back(
    engine: Engine, key_id: UUID
) -> None:
    waiter = Worker()
    with Session(engine) as blocker:
        blocker.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(update_worker, engine, key_id, waiter)
            with pytest.raises(owner.ApiKeyUpdateError) as error:
                result.result(timeout=10)
        assert error.value.code.endswith(".busy")
        assert error.value.__cause__ is None and error.value.__suppress_context__
        blocker.rollback()
    active, _, actions = snapshot(engine, key_id)
    assert active and actions == ()


def test_json_null_is_not_sql_null_on_migrated_schema(
    engine: Engine, key_id: UUID
) -> None:
    with Session(engine) as db:
        outcome = owner.update(
            db, command=update_command(key_id, ApiKeyUpdate(scopes=None))
        )
        assert outcome.scopes is None
        sql_null, encoded = db.execute(
            select(ApiKey.scopes.is_(None), cast(ApiKey.scopes, String)).where(
                ApiKey.id == key_id
            )
        ).one()
        assert sql_null is False and encoded == "null"
    assert snapshot(engine, key_id)[2] == ("auth.api_key.updated",)


@pytest.mark.parametrize("field", ["key_hash", "is_active"])
def test_nonnullable_null_has_no_update_or_audit(
    engine: Engine, key_id: UUID, field: str
) -> None:
    with Session(engine) as db:
        with pytest.raises(owner.ApiKeyUpdateError) as error:
            owner.update(
                db, command=update_command(key_id, ApiKeyUpdate(**{field: None}))
            )
        assert error.value.code.endswith(".storage_unavailable")
    active, key_hash, actions = snapshot(engine, key_id)
    assert active and key_hash == "inert-fixture-hash" and actions == ()
