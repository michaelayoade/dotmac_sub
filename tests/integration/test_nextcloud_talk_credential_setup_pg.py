"""Migrated PostgreSQL acceptance for credential transaction races and recovery.

Collect only locally; execute through the hosted integration lane. Providers
are synthetic, but installation locks, revisions, rollback and commit are real.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from queue import Queue
from threading import Event
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session, sessionmaker

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.integration_platform import IntegrationConfigRevision
from app.services import secrets
from app.services.events import EventType
from app.services.integrations import installations
from app.services.integrations import nextcloud_talk_installation as owner
from app.services.integrations.runtime import ValidationResult
from app.services.owner_commands import CommandContext
from tests.integration.test_credential_administration_concurrency import (
    wait_for_exact_lock,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def talk_engine(cloned_database) -> Iterator[Engine]:
    engine = create_engine(cloned_database("heads"))
    try:
        yield engine
    finally:
        engine.dispose()


def _setup(
    engine: Engine,
) -> tuple[sessionmaker[Session], owner.ConfigureTalkCredential]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as db:
        installation = installations.create_draft(
            db,
            connector_key="nextcloud.talk",
            name="PG credential proof",
            environment="test",
        )
        revision = installations.create_config_revision(
            db,
            installation_id=installation.id,
            config={
                "url": "https://next.example.test",
                "notifier_username": "old",
                "timeout_seconds": 12,
            },
            secret_refs={
                "app_password": "bao://secret/integrations/test-existing#app_password"
            },
        )
        installations.bind_capability(
            db,
            installation_id=installation.id,
            capability_id="collaboration.message.send.v1",
            scope={"staff": "approved"},
            policy={"approved_egress_hosts": ["next.example.test"], "default": True},
        )
        command = owner.ConfigureTalkCredential(
            context=CommandContext.system(
                actor="pytest:talk",
                scope=owner.TALK_CREDENTIAL_SCOPE,
                reason="PostgreSQL credential acceptance",
            ),
            installation_id=installation.id,
            expected_revision_id=revision.id,
            expected_pin=installations.ManifestPin(
                installation.connector_version, installation.manifest_digest
            ),
            operation_id=uuid4(),
            username="notifier",
            app_password=SecretStr("synthetic-pg-only-password"),
        )
        db.commit()
    return factory, command


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, str]] = {}
        self.calls: list[UUID] = []
        self.entered = Event()
        self.release = Event()
        self.pause = False

    def create(
        self, command: secrets.CreateTalkCredential
    ) -> secrets.ImmutableCredentialResult:
        self.calls.append(command.operation_id)
        reference = secrets.talk_credential_reference(command)
        material = (command.password.get_secret_value(), command.configuration_digest)
        replayed = reference in self.values
        if replayed and self.values[reference] != material:
            raise secrets._immutable_error("conflict")
        self.values[reference] = material
        self.entered.set()
        if self.pause:
            assert self.release.wait(15), (
                "coordinator did not release synthetic provider"
            )
        return secrets.ImmutableCredentialResult(reference, replayed)


def _providers(monkeypatch: pytest.MonkeyPatch) -> _Store:
    store = _Store()
    monkeypatch.setattr(owner, "create_talk_credential", store.create)
    monkeypatch.setattr(
        owner,
        "validate_installation_connection",
        lambda *args, **kwargs: ValidationResult(valid=True),
    )
    return store


def _capture_locks(monkeypatch: pytest.MonkeyPatch) -> Queue[int]:
    pids: Queue[int] = Queue()
    real = installations.lock_installation

    def lock(db: Session, installation_id: UUID | str):
        if db.info.pop("capture_talk_lock", False):
            pids.put(db.scalar(text("SELECT pg_backend_pid()")))
        return real(db, installation_id)

    monkeypatch.setattr(installations, "lock_installation", lock)
    return pids


def _save(factory: sessionmaker[Session], command: owner.ConfigureTalkCredential):
    with factory() as db:
        db.info["capture_talk_lock"] = True
        return owner.configure_talk_credential(db, command)


def _revision_count(db: Session, identity: UUID) -> int:
    return db.scalar(
        select(func.count())
        .select_from(IntegrationConfigRevision)
        .where(IntegrationConfigRevision.installation_id == identity)
    )


def test_competing_ui_operations_wait_then_stale_loser_never_writes_secret(
    talk_engine, monkeypatch
):
    factory, command = _setup(talk_engine)
    store = _providers(monkeypatch)
    pids = _capture_locks(monkeypatch)
    store.pause = True
    loser = replace(command, operation_id=uuid4(), username="other-notifier")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_save, factory, command)
        try:
            assert store.entered.wait(10)
            blocker = pids.get(timeout=3)
            second = pool.submit(_save, factory, loser)
            wait_for_exact_lock(
                talk_engine, waiter_pid=pids.get(timeout=3), blocker_pid=blocker
            )
        finally:
            store.release.set()
        winner = first.result(timeout=15)
        with pytest.raises(owner.TalkCredentialError) as rejected:
            second.result(timeout=15)
    assert rejected.value.code.endswith("_stale")
    assert store.calls == [command.operation_id]
    with factory() as db:
        row = installations.get_installation(db, command.installation_id)
        assert row.current_config_revision_id == winner.revision_id
        assert (
            row.current_config_revision.config_json["notifier_username"] == "notifier"
        )
        assert row.state == row.capability_bindings[0].state == "enabled"
        assert _revision_count(db, row.id) == 2


def test_legacy_config_winner_invalidates_ui_with_preloaded_orm_state(
    talk_engine, monkeypatch
):
    factory, command = _setup(talk_engine)
    store = _providers(monkeypatch)
    pids = _capture_locks(monkeypatch)
    ready, start = Event(), Event()

    def save_preloaded() -> owner.TalkCredentialOutcome:
        with factory() as ui:
            # Hold a strong reference across commit: expire_on_commit=False
            # preserves genuinely stale ORM state until the owner refreshes it.
            stale = installations.get_installation(ui, command.installation_id)
            assert stale.current_config_revision.id == command.expected_revision_id
            ui.commit()
            ready.set()
            assert start.wait(15)
            ui.info["capture_talk_lock"] = True
            return owner.configure_talk_credential(ui, command)

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(save_preloaded)
        try:
            assert ready.wait(10)
            with factory() as legacy:
                blocker = legacy.scalar(text("SELECT pg_backend_pid()"))
                replacement = installations.create_config_revision(
                    legacy,
                    installation_id=command.installation_id,
                    config={
                        "url": "https://next.example.test",
                        "notifier_username": "legacy",
                        "timeout_seconds": 30,
                    },
                    secret_refs={
                        "app_password": "bao://secret/integrations/test-legacy#app_password"
                    },
                )
                start.set()
                try:
                    wait_for_exact_lock(
                        talk_engine, waiter_pid=pids.get(timeout=3), blocker_pid=blocker
                    )
                finally:
                    legacy.commit()
        finally:
            start.set()
        with pytest.raises(owner.TalkCredentialError) as rejected:
            result.result(timeout=15)
    assert rejected.value.code.endswith("_stale")
    assert store.calls == []
    with factory() as db:
        row = installations.get_installation(db, command.installation_id)
        assert row.current_config_revision_id == replacement.id
        assert row.current_config_revision.config_json["timeout_seconds"] == 30
        assert _revision_count(db, row.id) == 2


def test_legacy_disable_waits_for_ui_and_refreshes_preloaded_binding(
    talk_engine, monkeypatch
):
    factory, command = _setup(talk_engine)
    store = _providers(monkeypatch)
    pids = _capture_locks(monkeypatch)
    ready, start = Event(), Event()
    store.pause = True

    def disable() -> UUID:
        with factory() as db:
            cached = installations.get_installation(db, command.installation_id)
            assert cached.current_config_revision.id == command.expected_revision_id
            assert cached.capability_bindings[0].state != "enabled"
            db.commit()
            ready.set()
            assert start.wait(15)
            db.info["capture_talk_lock"] = True
            updated = installations.disable_installation(
                db,
                installation_id=command.installation_id,
                reason="operator_after_ui",
                actor="pytest:legacy",
            )
            db.commit()
            return updated.current_config_revision_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        legacy = pool.submit(disable)
        try:
            assert ready.wait(10)
            saved = pool.submit(_save, factory, command)
            assert store.entered.wait(10)
            blocker = pids.get(timeout=3)
            start.set()
            wait_for_exact_lock(
                talk_engine, waiter_pid=pids.get(timeout=3), blocker_pid=blocker
            )
        finally:
            start.set()
            store.release.set()
        outcome = saved.result(timeout=15)
        assert legacy.result(timeout=15) == outcome.revision_id
    with factory() as db:
        row = installations.get_installation(db, command.installation_id)
        assert row.current_config_revision_id == outcome.revision_id
        assert row.state == row.capability_bindings[0].state == "disabled"
        assert row.state_reason == "operator_after_ui"
        assert row.current_config_revision.secret_refs["app_password"] in store.values
        assert _revision_count(db, row.id) == 2


def test_commit_failure_rolls_back_sql_and_same_operation_recovers_immutable_reference(
    talk_engine, monkeypatch
):
    factory, command = _setup(talk_engine)
    store = _providers(monkeypatch)
    with factory() as failed:

        def abort_at_commit(db: Session) -> None:
            # The provider has succeeded; the real PostgreSQL transaction now
            # aborts at the owner commit boundary, including its audit/outbox.
            assert len(store.values) == 1
            db.execute(text("SELECT 1 / 0"))

        event.listen(failed, "before_commit", abort_at_commit, once=True)
        with pytest.raises(DataError):
            owner.configure_talk_credential(failed, command)
        assert not failed.in_transaction()
    orphan_reference = next(iter(store.values))
    with factory() as db:
        row = installations.get_installation(db, command.installation_id)
        assert row.current_config_revision_id == command.expected_revision_id
        assert row.state != "enabled"
        assert _revision_count(db, row.id) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action
                    == "integration.nextcloud_talk.credentials.configure"
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(EventStore)
                .where(
                    EventStore.event_type
                    == EventType.integration_installation_talk_credential_configured.value
                )
            )
            == 0
        )
    with factory() as db:
        recovered = owner.configure_talk_credential(db, command)
    assert recovered.status is owner.TalkCredentialStatus.enabled
    assert store.calls == [command.operation_id, command.operation_id]
    assert list(store.values) == [orphan_reference]
    with factory() as db:
        row = installations.get_installation(db, command.installation_id)
        assert row.current_config_revision_id == recovered.revision_id
        assert (
            row.current_config_revision.secret_refs["app_password"] == orphan_reference
        )
        assert _revision_count(db, row.id) == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action
                    == "integration.nextcloud_talk.credentials.configure"
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(EventStore)
                .where(
                    EventStore.event_type
                    == EventType.integration_installation_talk_credential_configured.value
                )
            )
            == 1
        )
