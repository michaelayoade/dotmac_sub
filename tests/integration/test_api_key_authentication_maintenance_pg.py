"""Collect locally only; hosted real-Alembic heads own all concurrency acceptance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import event, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.schemas.auth import ApiKeyUpdate
from app.services import api_key_administration as owner
from app.services import staff_provisioning as staff
from tests.api_key_update_fixtures import update_command
from tests.integration import test_api_key_revocation_concurrency as revocations
from tests.integration import test_oidc_atomic_session_concurrency as races
from tests.integration.test_oidc_atomic_session_concurrency import Worker, _blocked_by
from tests.staff_deletion_fixtures import command as delete_staff_command
from tests.staff_deletion_fixtures import staff_fixture
from tests.web_api_key_fixtures import mutate_command

engine = races.engine
RAW = "inert-authentication-key"
LEGACY = "inert-authentication-legacy-digest"
PREFERRED = "hmac256:inert-authentication-preferred-digest"
ROTATED = "hmac256:inert-rotated-digest"


@pytest.fixture(autouse=True)
def inert_hashes(monkeypatch):
    monkeypatch.setattr(
        "app.services.auth.hash_api_key_candidates",
        lambda raw: [PREFERRED, LEGACY] if raw == RAW else [ROTATED],
    )
    monkeypatch.setattr("app.services.auth.hash_api_key", lambda raw: ROTATED)
    monkeypatch.setattr("secrets.token_urlsafe", lambda size: "inert-rotated-key")


@pytest.fixture
def target(engine: Engine) -> tuple[UUID, UUID]:
    with Session(engine) as db:
        user_id, key_id = staff_fixture(db)
        db.execute(
            sql_update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(
                key_hash=LEGACY,
                scopes=["audit:read"],
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db.commit()
        return user_id, key_id


def authenticate_worker(
    engine: Engine,
    worker: Worker,
    *,
    pause=False,
    audience=owner.ApiKeyAuthenticationAudience.GENERAL,
):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        return owner.authenticate(
            db, command=owner.AuthenticateApiKeyCommand(RAW, audience)
        )


def mutation_worker(
    engine: Engine,
    target: tuple[UUID, UUID],
    worker: Worker,
    operation: str,
    *,
    pause=False,
):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        _, key_id = target
        if operation == "rotate":
            return owner.web_mutate(
                db, command=mutate_command(key_id, owner.WebApiKeyAction.ROTATE)
            )
        if operation == "revoke":
            return owner.revoke(db, command=revocations.command(key_id))
        if operation == "scope":
            return owner.update(
                db,
                command=update_command(key_id, ApiKeyUpdate(scopes=["billing:read"])),
            )
        return staff.delete_staff_account(db, command=delete_staff_command(target[0]))


def inspect_key(engine: Engine, key_id: UUID):
    with Session(engine) as db:
        row = db.execute(
            select(
                ApiKey.key_hash, ApiKey.is_active, ApiKey.scopes, ApiKey.last_used_at
            ).where(ApiKey.id == key_id)
        ).first()
        actions = tuple(
            db.scalars(
                select(AuditEvent.action).where(AuditEvent.entity_id == str(key_id))
            )
        )
        return row, actions


@pytest.mark.parametrize("first", ["authentication", "mutation"])
@pytest.mark.parametrize("operation", ["rotate", "revoke", "scope", "delete"])
def test_both_orderings_exact_key_maintenance_arbitration(
    engine: Engine, target, first, operation
):
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        if first == "authentication":
            one = pool.submit(authenticate_worker, engine, blocker, pause=True)
        else:
            one = pool.submit(
                mutation_worker, engine, target, blocker, operation, pause=True
            )
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            if first == "authentication":
                two = pool.submit(mutation_worker, engine, target, waiter, operation)
            else:
                two = pool.submit(
                    authenticate_worker,
                    engine,
                    waiter,
                    audience=owner.ApiKeyAuthenticationAudience.AUDIT,
                )
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        a, b = one.result(timeout=15), two.result(timeout=15)
    auth = a if first == "authentication" else b
    assert bool(auth.principal) is (first == "authentication")
    row, actions = inspect_key(engine, target[1])
    assert actions.count("auth.api_key.hash_upgraded") == int(first == "authentication")
    if operation == "delete":
        assert row is None and "auth.api_key.deleted" in actions
    elif operation == "rotate":
        assert row.key_hash == ROTATED
        with Session(engine) as db:
            assert (
                owner.authenticate(
                    db,
                    command=owner.AuthenticateApiKeyCommand(
                        RAW, owner.ApiKeyAuthenticationAudience.GENERAL
                    ),
                ).principal
                is None
            )
    elif operation == "revoke":
        assert not row.is_active
    else:
        assert row.scopes == ["billing:read"]
        assert row.key_hash == (PREFERRED if first == "authentication" else LEGACY)


@pytest.mark.parametrize("modern", [False, True])
def test_competing_equivalent_upgrades_audit_once_and_accept_fresh_projection(
    engine: Engine, target, modern
):
    if modern:
        with Session(engine) as db:
            db.execute(
                sql_update(ApiKey)
                .where(ApiKey.id == target[1])
                .values(key_hash=PREFERRED)
            )
            db.commit()
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(authenticate_worker, engine, blocker, pause=True)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            two = pool.submit(authenticate_worker, engine, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        assert one.result(timeout=15).principal.key_id == target[1]
        assert two.result(timeout=15).principal.key_id == target[1]
    row, actions = inspect_key(engine, target[1])
    assert row.key_hash == PREFERRED and row.last_used_at is not None
    assert actions.count("auth.api_key.hash_upgraded") == int(not modern)


def test_concurrent_newer_touch_survives_waiting_upgrade(engine: Engine, target):
    blocker, waiter = Worker(), Worker()
    newer = datetime.now(UTC) + timedelta(minutes=1)

    def touch():
        with engine.connect() as connection, Session(connection) as db:
            blocker.identify(db)
            db.execute(
                sql_update(ApiKey)
                .where(ApiKey.id == target[1])
                .values(last_used_at=newer)
            )
            blocker.ready.set()
            assert blocker.release.wait(8)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(touch)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            two = pool.submit(authenticate_worker, engine, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        one.result(timeout=15)
        assert two.result(timeout=15).principal.key_id == target[1]
    row, actions = inspect_key(engine, target[1])
    assert row.key_hash == PREFERRED and row.last_used_at == newer
    assert actions.count("auth.api_key.hash_upgraded") == 1


def test_expiry_reached_during_lock_wait_cannot_upgrade_or_grant(
    engine: Engine, target
):
    blocker, waiter = Worker(), Worker()
    expiry = datetime.now(UTC) + timedelta(seconds=2)

    # Keep expiry uncommitted: the initial authentication read always sees
    # the old day-ahead deadline and reaches the exact key wait before denial.
    def hold():
        with engine.connect() as connection, Session(connection) as db:
            blocker.identify(db)
            db.execute(
                sql_update(ApiKey)
                .where(ApiKey.id == target[1])
                .values(expires_at=expiry)
            )
            blocker.ready.set()
            assert blocker.release.wait(8)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(hold)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            two = pool.submit(authenticate_worker, engine, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
            # Explicit wall-clock barrier; do not assume expiry from sleep.
            from time import monotonic

            deadline = monotonic() + 3
            while datetime.now(UTC) <= expiry and monotonic() < deadline:
                Event().wait(0.01)
            assert datetime.now(UTC) > expiry
        finally:
            blocker.release.set()
        one.result(timeout=15)
        assert two.result(timeout=15).principal is None
    row, actions = inspect_key(engine, target[1])
    assert row.key_hash == LEGACY and row.last_used_at is None
    assert "auth.api_key.hash_upgraded" not in actions


def test_audit_failure_restores_hash_and_touch(engine: Engine, target, monkeypatch):
    original = owner.stage_audit_event

    def fail(db, **kwargs):
        original(db, **kwargs)
        db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with Session(engine) as db, pytest.raises(owner.ApiKeyAuthenticationError) as exc:
        owner.authenticate(
            db,
            command=owner.AuthenticateApiKeyCommand(
                RAW, owner.ApiKeyAuthenticationAudience.GENERAL
            ),
        )
    assert exc.value.code.endswith("authentication_unavailable")
    row, actions = inspect_key(engine, target[1])
    assert row.key_hash == LEGACY and row.last_used_at is None
    assert "auth.api_key.hash_upgraded" not in actions


def test_maintenance_lock_timeout_has_no_partial_audit_or_update(
    engine: Engine, target
):
    blocker, waiter = Worker(), Worker()

    def hold():
        with engine.connect() as connection, Session(connection) as db:
            blocker.identify(db)
            db.execute(
                select(ApiKey.id).where(ApiKey.id == target[1]).with_for_update()
            )
            blocker.ready.set()
            assert blocker.release.wait(9)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(hold)
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            two = pool.submit(authenticate_worker, engine, waiter)
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
            with pytest.raises(owner.ApiKeyAuthenticationError) as exc:
                two.result(timeout=8)
            assert exc.value.code.endswith("authentication_busy")
        finally:
            blocker.release.set()
        one.result(timeout=15)
    row, actions = inspect_key(engine, target[1])
    assert row.key_hash == LEGACY and row.last_used_at is None
    assert "auth.api_key.hash_upgraded" not in actions
