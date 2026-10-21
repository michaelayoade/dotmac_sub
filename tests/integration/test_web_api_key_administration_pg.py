"""Collect locally only; hosted migrated PostgreSQL owns acceptance."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.services import api_key_administration as owner
from tests.integration import test_api_key_revocation_concurrency as revocations
from tests.integration import test_api_key_update_pg as updates
from tests.integration import test_oidc_atomic_session_concurrency as races
from tests.integration.test_oidc_atomic_session_concurrency import Worker, _blocked_by
from tests.web_api_key_fixtures import create_command, mutate_command

engine = races.engine
key_id = revocations.key_id


def rotate_worker(
    engine: Engine, key_id: UUID, worker: Worker, pause: bool = False
) -> owner.WebApiKeyOutcome:
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        return owner.web_mutate(
            db,
            command=mutate_command(key_id, owner.WebApiKeyAction.ROTATE, invalid=True),
        )


def test_web_rotate_then_json_revoke_serialize_exact_key(
    engine: Engine, key_id: UUID
) -> None:
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(rotate_worker, engine, key_id, blocker, True)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            second = pool.submit(revocations.revoke_worker, engine, key_id, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        assert first.result(timeout=15).secret
        second.result(timeout=15)
    active, key_hash, actions = updates.snapshot(engine, key_id)
    assert not active and key_hash != "inert-fixture-hash"
    assert actions == ("auth.api_key.rotated", "auth.api_key.revoked")


def test_json_revoke_then_web_rotate_refreshes_and_refuses(
    engine: Engine, key_id: UUID
) -> None:
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(revocations.revoke_worker, engine, key_id, blocker, True)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            second = pool.submit(rotate_worker, engine, key_id, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        first.result(timeout=15)
        assert second.result(timeout=15).secret is None
    active, key_hash, actions = updates.snapshot(engine, key_id)
    assert not active and key_hash == "inert-fixture-hash"
    assert actions == ("auth.api_key.revoked",)


def test_web_rotation_timeout_is_sanitized_and_rolled_back(
    engine: Engine, key_id: UUID
) -> None:
    with Session(engine) as blocker:
        blocker.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(rotate_worker, engine, key_id, Worker())
            with pytest.raises(owner.WebApiKeyError) as error:
                result.result(timeout=10)
        assert error.value.code.endswith(".busy")
        assert error.value.__cause__ is None and error.value.__suppress_context__
        blocker.rollback()
    assert updates.snapshot(engine, key_id) == (True, "inert-fixture-hash", ())


def test_web_create_canonical_audit_failure_rolls_back(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = owner.stage_audit_event

    def fail(*args, **kwargs):
        stage(*args, **kwargs)
        raise RuntimeError("inert audit failure")

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with Session(engine) as db:
        with pytest.raises(RuntimeError, match="inert audit failure"):
            owner.web_create(
                db,
                command=create_command(
                    subscriber_id=None, label="inert-web-create", expires_in=None
                ),
            )
        assert not db.scalars(
            select(ApiKey).where(ApiKey.label == "inert-web-create")
        ).all()
        assert not db.scalars(
            select(AuditEvent).where(AuditEvent.action == "auth.api_key.created")
        ).all()
