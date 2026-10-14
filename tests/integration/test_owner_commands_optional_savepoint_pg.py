"""Real migrated PostgreSQL evidence for the existing optional owner boundary."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IllegalStateChangeError, IntegrityError
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceProjection
from app.services import owner_commands
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    OwnerCommandError,
    execute_owner_command,
    execute_owner_savepoint,
)

DEFINITION = OwnerCommandDefinition(
    owner="network.device_projection",
    concern="device_projections materialised table",
    name="optional_savepoint_pg_canary",
)


@dataclass
class Boundary:
    engine: Engine
    db: Session
    prefix: str

    def add(self, suffix: str) -> None:
        self.db.add(
            DeviceProjection(
                device_type="test",
                source_id=self.prefix + suffix,
                operational_status="not_working",
            )
        )

    def visible(self) -> set[str]:
        # A separately checked-out connection sees only committed records.
        with self.engine.connect() as observer:
            return set(
                observer.scalars(
                    select(DeviceProjection.source_id).where(
                        DeviceProjection.source_id.startswith(self.prefix)
                    )
                )
            )

    def run(self, operation: Callable[[], None]) -> None:
        execute_owner_command(
            self.db,
            definition=DEFINITION,
            context=CommandContext.system(
                actor="pytest:optional-savepoint", scope="network", reason="PG canary"
            ),
            operation=operation,
        )

    def clean(self) -> None:
        assert not self.db.in_transaction()
        assert not any(key.startswith("_dotmac_") for key in self.db.info)


@pytest.fixture
def boundary(engine: Engine) -> Iterator[Boundary]:
    # Root conftest requires the real migrated schema; integration conftest
    # refuses SQLite. Use an engine-bound Session, without an outer harness
    # transaction, so the independent observer can prove actual publication.
    with Session(engine) as db:
        subject = Boundary(engine, db, "owner-sp-" + uuid4().hex + "-")
        try:
            yield subject
        finally:
            db.close()
            with engine.begin() as cleanup:
                cleanup.execute(
                    delete(DeviceProjection).where(
                        DeviceProjection.source_id.startswith(subject.prefix)
                    )
                )


def test_optional_release_publishes_only_at_owner_commit(boundary: Boundary) -> None:
    def operation() -> None:
        boundary.add("root")
        execute_owner_savepoint(boundary.db, lambda: boundary.add("optional"))
        assert boundary.visible() == set()

    boundary.run(operation)
    assert boundary.visible() == {
        boundary.prefix + "root",
        boundary.prefix + "optional",
    }
    boundary.clean()


def test_real_unique_flush_conflict_preserves_root_and_local_guc(
    boundary: Boundary,
) -> None:
    def duplicate() -> None:
        boundary.add("root")
        boundary.db.flush()

    def operation() -> None:
        root = boundary.db.get_transaction()
        boundary.db.execute(text("SET LOCAL app.owner_savepoint_canary = 'preserved'"))
        boundary.add("root")
        boundary.db.flush()
        with pytest.raises(IntegrityError):
            execute_owner_savepoint(boundary.db, duplicate)
        assert boundary.db.get_transaction() is root
        assert root is not None and root.is_active
        assert not boundary.db.in_nested_transaction()
        assert (
            boundary.db.scalar(text("SHOW app.owner_savepoint_canary")) == "preserved"
        )
        assert boundary.visible() == set()
        boundary.add("evidence")

    boundary.run(operation)
    assert boundary.visible() == {
        boundary.prefix + "root",
        boundary.prefix + "evidence",
    }
    boundary.clean()


@pytest.mark.parametrize("target", ["session", "root", "nested"])
@pytest.mark.parametrize("completion", ["commit", "rollback"])
@pytest.mark.parametrize("swallow", [False, True])
def test_body_completion_cannot_publish_even_when_swallowed(
    boundary: Boundary, target: str, completion: str, swallow: bool
) -> None:
    def participant() -> None:
        boundary.add("illegal")
        boundary.db.flush()
        handle = {
            "session": boundary.db,
            "root": boundary.db.get_transaction(),
            "nested": boundary.db.get_nested_transaction(),
        }[target]
        assert handle is not None
        try:
            getattr(handle, completion)()
        except OwnerCommandError as rejection:
            assert (
                rejection.code
                == "network.device_projection.nested_transaction_completion"
            )
            if not swallow:
                raise

    def operation() -> None:
        boundary.add("root")
        try:
            execute_owner_savepoint(boundary.db, participant)
        except OwnerCommandError as rejection:
            assert (
                rejection.code
                == "network.device_projection.nested_transaction_completion"
            )
            if not swallow:
                raise
        assert boundary.visible() == set()

    with pytest.raises(OwnerCommandError) as captured:
        boundary.run(operation)
    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert boundary.visible() == set()
    boundary.clean()
    boundary.run(lambda: boundary.add("fresh"))
    assert boundary.visible() == {boundary.prefix + "fresh"}
    boundary.clean()


@pytest.mark.parametrize("phase", ["before_commit", "before_flush"])
@pytest.mark.parametrize("before_guard", [False, True])
@pytest.mark.parametrize("target", ["session", "root"])
@pytest.mark.parametrize("completion", ["commit", "rollback"])
@pytest.mark.parametrize("swallow", [False, True])
def test_release_reentrant_completion_is_denied_and_later_owner_failure_rolls_back(
    boundary: Boundary,
    phase: str,
    before_guard: bool,
    target: str,
    completion: str,
    swallow: bool,
) -> None:
    attempted = False
    denied: list[OwnerCommandError | IllegalStateChangeError] = []
    later_failure = RuntimeError("later owner operation failed")
    # Session completion targets the preparing child and is rejected by
    # SQLAlchemy before events. Root rollback likewise tries to close that
    # preparing child. Root commit reaches our guard unless its before-guard
    # callback first consumes the release claim, then tries to commit the child.
    expected_type = (
        OwnerCommandError
        if target == "root"
        and completion == "commit"
        and (phase == "before_flush" or not before_guard)
        else IllegalStateChangeError
    )

    def attack(session: Session, *unused: object) -> None:
        nonlocal attempted
        if session is not boundary.db or attempted:
            return
        # BODY and begin_nested's preflush must remain ordinary operations.
        if not releasing[0]:
            return
        attempted = True
        handle = session if target == "session" else session.get_transaction()
        assert handle is not None
        try:
            getattr(handle, completion)()
        except (OwnerCommandError, IllegalStateChangeError) as rejection:
            assert type(rejection) is expected_type
            assert rejection.code == (
                "network.device_projection.nested_transaction_completion"
                if expected_type is OwnerCommandError
                else "isce"
            )
            denied.append(rejection)
            assert boundary.visible() == set()
            if not swallow:
                raise
        else:
            pytest.fail("Reentrant completion returned without being denied")
        assert boundary.visible() == set()

    releasing = [False]

    def participant() -> None:
        boundary.add("optional")
        releasing[0] = True

    def operation() -> None:
        boundary.add("root")
        try:
            execute_owner_savepoint(boundary.db, participant)
        except (OwnerCommandError, IllegalStateChangeError):
            if not swallow:
                raise
        assert attempted
        assert denied
        assert boundary.visible() == set()
        # SQLAlchemy can deny same-handle reentry before dispatching events.
        # Even when that already-denied error is swallowed and release succeeds,
        # the writes must remain owned by this later root failure.
        raise later_failure

    event.listen(Session, phase, attack, insert=before_guard)
    try:
        with pytest.raises(RuntimeError if swallow else expected_type) as captured:
            boundary.run(operation)
    finally:
        event.remove(Session, phase, attack)
    assert attempted
    assert denied
    assert captured.value is (later_failure if swallow else denied[0])
    assert boundary.visible() == set()
    boundary.clean()
    boundary.run(lambda: boundary.add("fresh"))
    assert boundary.visible() == {boundary.prefix + "fresh"}
    boundary.clean()


@pytest.mark.parametrize("phase", ["after_commit", "after_transaction_end"])
@pytest.mark.parametrize("swallow", [False, True])
@pytest.mark.parametrize("secondary_failure", [False, True])
def test_release_listener_error_preserves_first_and_unproven_cleanup_aborts(
    boundary: Boundary, phase: str, swallow: bool, secondary_failure: bool
) -> None:
    first = ValueError("first optional release listener failure")
    cause = LookupError("original diagnostic cause")
    fired = False

    def fail(session: Session, *args: object) -> None:
        nonlocal fired
        if session is not boundary.db or fired:
            return
        child = session.info.get(owner_commands._ACTIVE_SAVEPOINT_KEY)
        if child is None or (phase == "after_transaction_end" and args[0] is not child):
            return
        fired = True
        raise first from cause

    def fail_cleanup(session: Session, transaction: object) -> None:
        if session is boundary.db and fired and secondary_failure:
            raise RuntimeError("secondary cleanup failure")

    def operation() -> None:
        boundary.add("root")
        try:
            execute_owner_savepoint(boundary.db, lambda: boundary.add("optional"))
        except ValueError as captured:
            assert captured is first
            assert captured.__cause__ is cause
            if not swallow:
                raise
        assert boundary.visible() == set()

    event.listen(Session, phase, fail)
    event.listen(Session, "after_transaction_end", fail_cleanup)
    try:
        with pytest.raises(OwnerCommandError if swallow else ValueError) as captured:
            boundary.run(operation)
        if not swallow:
            assert captured.value is first
            assert captured.value.__cause__ is cause
    finally:
        event.remove(Session, phase, fail)
        event.remove(Session, "after_transaction_end", fail_cleanup)
    assert fired
    assert first.__cause__ is cause
    if secondary_failure:
        assert any(
            "RuntimeError" in note
            for error in (first, captured.value)
            for note in getattr(error, "__notes__", ())
        )
    assert boundary.visible() == set()
    boundary.clean()
    boundary.run(lambda: boundary.add("fresh"))
    assert boundary.visible() == {boundary.prefix + "fresh"}
    boundary.clean()
