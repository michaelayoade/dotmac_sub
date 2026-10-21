"""Collect locally only; real migrated PostgreSQL is the locking authority."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.services import api_key_administration as owner
from tests.integration import test_oidc_atomic_session_concurrency as races
from tests.integration.test_oidc_atomic_session_concurrency import Worker, _blocked_by
from tests.test_api_key_administration import command

engine = races.engine


@pytest.fixture
def key_id(engine: Engine) -> UUID:
    with Session(engine) as db:
        key = ApiKey(
            id=uuid4(),
            label="inert-fixture",
            key_hash="inert-fixture-hash",
            is_active=True,
        )
        db.add(key)
        db.commit()
        return key.id


def revoke_worker(
    engine: Engine, key_id: UUID, worker: Worker, pause: bool = False
) -> owner.ApiKeyRevocationOutcome:
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        return owner.revoke(db, command=command(key_id))


def test_concurrent_repeat_waits_for_exact_key(engine: Engine, key_id: UUID) -> None:
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(revoke_worker, engine, key_id, blocker, True)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            second = pool.submit(revoke_worker, engine, key_id, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        first_result = first.result(timeout=15)
        second_result = second.result(timeout=15)
    assert second_result.revoked_at >= first_result.revoked_at
    with Session(engine) as db:
        assert not db.get(ApiKey, key_id).is_active
        audits = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_id == str(key_id))
            .order_by(AuditEvent.occurred_at)
        ).all()
        assert len(audits) == 2
        assert audits[0].details["old_is_active"] is True
        assert audits[1].details["old_is_active"] is False


def test_lock_timeout_rolls_back_state_and_audit(engine: Engine, key_id: UUID) -> None:
    waiter = Worker()
    with Session(engine) as blocker:
        blocker.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(revoke_worker, engine, key_id, waiter)
            with pytest.raises(OperationalError) as error:
                result.result(timeout=10)
        assert (
            getattr(error.value.orig, "sqlstate", None) == "55P03"
            or getattr(error.value.orig, "pgcode", None) == "55P03"
        )
        blocker.rollback()
    with Session(engine) as db:
        assert db.get(ApiKey, key_id).is_active
        assert not db.scalars(
            select(AuditEvent).where(AuditEvent.entity_id == str(key_id))
        ).all()
