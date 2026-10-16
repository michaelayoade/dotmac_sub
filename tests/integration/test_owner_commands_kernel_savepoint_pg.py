"""Hosted, migrated PostgreSQL proof of Sub/Kernel transaction composition.

Reuse the existing isolated actual-login fixture; no role impersonation, schema
substitute or Kernel protocol mock is used. Synthetic numbering rows exercise
mechanics under an existing registered Sub owner, not a product numbering cutover.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import TypeVar
from uuid import UUID, uuid4

import psycopg
import pytest
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.transactions import (
    ConflictSavepointPhase,
    ConflictSavepointProtocolError,
    claim_conflict_savepoint_commit,
    conflict_savepoint,
    conflict_savepoint_requires_rollback,
    current_conflict_savepoint,
)
from dotmac_numbering.models import NumberSeries
from dotmac_numbering.service import (
    Allocation,
    SeriesConfiguration,
    allocate,
    configure_series,
)
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import (
    DBAPIError,
    IllegalStateChangeError,
    IntegrityError,
    InvalidRequestError,
)
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import StaticPool

from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    OwnerCommandError,
    current_owner_root_transaction,
    execute_owner_command,
    execute_owner_savepoint,
)
from tests.integration.test_billing_runtime_login_postgres import (
    RuntimeConnections,
    runtime_connections,  # noqa: F401 -- retained hosted migrated actual-login fixture
)

ResultT = TypeVar("ResultT")
REFERENCE_DATE = date(2026, 10, 16)
DEFINITION = OwnerCommandDefinition(
    owner="network.device_projection",
    concern="device_projections materialised table",
    name="kernel_savepoint_pg_canary",
)
BOUNDARY_CODE = "network.device_projection.nested_transaction_completion"
REFUSALS = (OwnerCommandError, ConflictSavepointProtocolError, InvalidRequestError)


@dataclass
class KernelBoundary:
    engine: Engine
    db: Session
    admin: psycopg.Connection
    prefix: str
    other_tenant: UUID

    @property
    def scope(self) -> TenantScope:
        return TenantScope(tenant_id=OPERATOR_TENANT_ID)

    def run(self, operation: Callable[[], ResultT]) -> ResultT:
        def scoped() -> ResultT:
            assert self.db.execute(text("SELECT session_user, current_user")).one() == (
                "app_user",
                "app_user",
            )
            self.db.execute(
                text("SELECT set_config('app.current_tenant', :tenant, true)"),
                {"tenant": str(OPERATOR_TENANT_ID)},
            )
            return operation()

        return execute_owner_command(
            self.db,
            definition=DEFINITION,
            context=CommandContext.system(
                actor="pytest:kernel-composition",
                scope="network",
                reason="Hosted PG canary",
            ),
            operation=scoped,
        )

    def configure(self, suffix: str) -> None:
        configure_series(
            self.db,
            scope=self.scope,
            configuration=SeriesConfiguration(
                series_code=self.prefix + suffix,
                prefix="CAN",
                separator="",
                start_value=41,
            ),
        )

    def pending(self, suffix: str, *, tenant: UUID = OPERATOR_TENANT_ID) -> None:
        self.db.add(NumberSeries(tenant_id=tenant, series_code=self.prefix + suffix))

    def allocation(self, key: str, *, on: date = REFERENCE_DATE) -> Allocation:
        return allocate(
            self.db,
            scope=self.scope,
            series_code=self.prefix + "series",
            reference_date=on,
            idempotency_key=self.prefix + key,
        )

    def visible(self) -> set[str]:
        # The admin observer is another backend, never a StaticPool checkout.
        return {
            row[0]
            for row in self.admin.execute(
                "SELECT series_code FROM mod_numbering.number_series WHERE series_code LIKE %s",
                (self.prefix + "%",),
            )
        }

    def preserved(
        self, root: SessionTransaction, child: SessionTransaction | None = None
    ) -> None:
        assert self.db.get_transaction() is root and root.is_active
        assert self.db.get_nested_transaction() is child
        assert self.db.scalar(
            text("SELECT current_setting('app.current_tenant')")
        ) == str(OPERATOR_TENANT_ID)
        assert not conflict_savepoint_requires_rollback(self.db, expected_root=root)

    def clean(self) -> None:
        assert not self.db.in_transaction()
        assert current_conflict_savepoint(self.db) is None
        assert not any(key.startswith("_dotmac_") for key in self.db.info)


@pytest.fixture
def kernel_boundary(
    runtime_connections: RuntimeConnections,  # noqa: F811 -- imported fixture
) -> Iterator[KernelBoundary]:
    runtime_connections.runtime.rollback()
    other = uuid4()
    runtime_connections.admin.execute(
        "INSERT INTO public.tenants (id, slug, name, is_active) VALUES (%s, %s, %s, true)",
        (other, "kernel-canary-" + other.hex, "Synthetic Kernel isolation tenant"),
    )
    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: runtime_connections.runtime,
        poolclass=StaticPool,
    )
    try:
        with Session(engine) as db:
            yield KernelBoundary(
                engine,
                db,
                runtime_connections.admin,
                "kernel-" + uuid4().hex + "-",
                other,
            )
    finally:
        # The fixture retains custody of its connection and restores the role.
        engine.dispose(close=False)
        runtime_connections.runtime.rollback()


def test_numbering_public_owner_api_releases_without_publishing_and_exact_replay(
    kernel_boundary: KernelBoundary,
) -> None:
    b = kernel_boundary

    def operation() -> None:
        root = current_owner_root_transaction(b.db)
        b.configure("series")
        first = b.allocation("exact")
        replay = b.allocation("exact")
        assert first.value == replay.value == 41
        assert first.formatted_number == replay.formatted_number == "CAN000041"
        assert not first.replayed and replay.replayed
        # Import only at hosted execution, as the released service does itself.
        from dotmac_kernel.idempotency import IdempotencyConflict

        with pytest.raises(IdempotencyConflict):
            b.allocation("exact", on=date(2026, 10, 17))
        b.preserved(root)
        assert current_conflict_savepoint(b.db) is None
        assert not claim_conflict_savepoint_commit(b.db, expected_root=root)
        assert b.visible() == set()

    b.run(operation)
    assert b.visible() == {b.prefix + "series"}
    assert b.admin.execute(
        "SELECT allocated_value FROM mod_numbering.allocation_receipts WHERE series_code=%s",
        (b.prefix + "series",),
    ).fetchall() == [(41,)]
    b.clean()
    assert b.run(lambda: b.allocation("exact")).replayed
    b.clean()


@dataclass(frozen=True)
class RollbackObservation:
    phase: ConflictSavepointPhase
    exact_savepoint: bool
    internal_child: bool
    savepoint_active: bool
    still_current: bool
    root: SessionTransaction


@pytest.mark.parametrize(
    "explicit_flush", [True, False], ids=["body-flush", "release-flush"]
)
def test_real_unique_conflict_event_trace_preserves_exact_root_and_rls(
    kernel_boundary: KernelBoundary,
    explicit_flush: bool,
) -> None:
    b = kernel_boundary
    observed: list[RollbackObservation] = []

    def trace(db: Session, previous: SessionTransaction) -> None:
        if db is not b.db:
            return
        state = current_conflict_savepoint(db)
        if state is not None:
            observed.append(
                RollbackObservation(
                    state.phase,
                    previous is state.savepoint,
                    previous.parent is state.savepoint and not previous.nested,
                    state.savepoint.is_active,
                    db.get_nested_transaction() is state.savepoint,
                    state.root,
                )
            )

    def operation() -> None:
        root = current_owner_root_transaction(b.db)
        b.configure("root")
        with pytest.raises(IntegrityError):
            with conflict_savepoint(b.db):
                state = current_conflict_savepoint(b.db)
                assert state is not None and state.root is root
                b.pending("root")  # the migrated tenant+series unique constraint
                if explicit_flush:
                    b.db.flush()
        assert len(observed) == 2
        failed, rolled_back = observed
        assert failed.phase is (
            ConflictSavepointPhase.BODY
            if explicit_flush
            else ConflictSavepointPhase.RELEASE
        )
        assert failed.internal_child and not failed.exact_savepoint
        assert not failed.savepoint_active and failed.still_current
        assert rolled_back.phase is ConflictSavepointPhase.ROLLBACK
        assert rolled_back.exact_savepoint and not rolled_back.internal_child
        assert all(item.root is root for item in observed)
        b.preserved(root)
        assert current_conflict_savepoint(b.db) is None
        assert not claim_conflict_savepoint_commit(b.db, expected_root=root)
        assert b.visible() == set()
        b.configure("failure-evidence")

    event.listen(Session, "after_soft_rollback", trace)
    try:
        b.run(operation)
    finally:
        event.remove(Session, "after_soft_rollback", trace)
    assert b.visible() == {b.prefix + "root", b.prefix + "failure-evidence"}
    b.clean()
    b.run(lambda: b.configure("fresh"))
    b.clean()


@pytest.mark.parametrize("target", ["session", "root", "nested"])
@pytest.mark.parametrize("completion", ["commit", "rollback"])
@pytest.mark.parametrize("swallow", [False, True])
def test_body_malicious_completion_never_publishes_even_when_swallowed(
    kernel_boundary: KernelBoundary,
    target: str,
    completion: str,
    swallow: bool,
) -> None:
    b = kernel_boundary
    attempted = False

    def operation() -> None:
        nonlocal attempted
        root = current_owner_root_transaction(b.db)
        b.configure("root")
        try:
            with conflict_savepoint(b.db):
                state = current_conflict_savepoint(b.db)
                assert state is not None and state.phase is ConflictSavepointPhase.BODY
                assert not claim_conflict_savepoint_commit(b.db, expected_root=root)
                b.configure("child")
                handle = {"session": b.db, "root": root, "nested": state.savepoint}[
                    target
                ]
                attempted = True
                try:
                    getattr(handle, completion)()
                except REFUSALS:
                    assert b.visible() == set()
                    if not swallow:
                        raise
        except REFUSALS:
            if not swallow:
                raise
        assert b.visible() == set()

    with pytest.raises(REFUSALS) as captured:
        b.run(operation)
    if swallow:
        assert isinstance(captured.value, OwnerCommandError)
        assert captured.value.code == BOUNDARY_CODE
    assert attempted and b.visible() == set()
    b.clean()
    b.run(lambda: b.configure("fresh"))
    assert b.visible() == {b.prefix + "fresh"}
    b.clean()


@pytest.mark.parametrize("listener", ["before_commit", "before_flush"])
@pytest.mark.parametrize("target", ["session", "root", "nested"])
@pytest.mark.parametrize("completion", ["commit", "rollback"])
@pytest.mark.parametrize("swallow", [False, True])
def test_kernel_release_claim_is_consumed_before_reentrant_listener_or_flush(
    kernel_boundary: KernelBoundary,
    listener: str,
    target: str,
    completion: str,
    swallow: bool,
) -> None:
    b = kernel_boundary
    attempts: list[BaseException] = []
    later = RuntimeError("later owner failure owns every unpublished write")

    def attack(db: Session, *unused: object) -> None:
        state = current_conflict_savepoint(db)
        if (
            db is not b.db
            or attempts
            or state is None
            or state.phase is not ConflictSavepointPhase.RELEASE
        ):
            return
        # Registered after Sub's guard: the real helper claim is already spent.
        assert not claim_conflict_savepoint_commit(db, expected_root=state.root)
        handle = {"session": db, "root": state.root, "nested": state.savepoint}[target]
        try:
            getattr(handle, completion)()
        except (OwnerCommandError, IllegalStateChangeError) as denied:
            attempts.append(denied)
            assert db.get_transaction() is state.root and state.root.is_active
            assert db.get_nested_transaction() is state.savepoint
            assert b.visible() == set()
            if not swallow:
                raise
        else:
            pytest.fail("Reentrant completion was accepted")

    def operation() -> None:
        b.configure("root")
        try:
            with conflict_savepoint(b.db):
                b.pending("child")  # deferred flush occurs in RELEASE
        except REFUSALS:
            if not swallow:
                raise
        assert attempts and b.visible() == set()
        if swallow and isinstance(attempts[0], OwnerCommandError):
            # Sub observed the violation: its persistent poison must refuse an
            # otherwise successful owner, without any later synthetic failure.
            return
        # SQLAlchemy can reject reentry before dispatching Sub's guard. Such an
        # unobservable attempt proves isolation through a later owner failure.
        raise later

    event.listen(Session, listener, attack)
    try:
        with pytest.raises((RuntimeError, *REFUSALS)) as captured:
            b.run(operation)
        if swallow and isinstance(attempts[0], OwnerCommandError):
            assert isinstance(captured.value, OwnerCommandError)
            assert captured.value.code == BOUNDARY_CODE
        else:
            assert captured.value is (later if swallow else attempts[0])
    finally:
        event.remove(Session, listener, attack)
    assert attempts and b.visible() == set()
    b.clean()
    b.run(lambda: b.configure("fresh"))
    b.clean()


def test_kernel_inside_optional_restores_exact_child_and_root_with_failure_evidence(
    kernel_boundary: KernelBoundary,
) -> None:
    b = kernel_boundary
    original = ValueError("ordinary optional consequence failure")

    def operation() -> None:
        root = current_owner_root_transaction(b.db)
        b.configure("root")

        def optional() -> None:
            outer = b.db.get_nested_transaction()
            assert outer is not None
            with conflict_savepoint(b.db):
                state = current_conflict_savepoint(b.db)
                assert state is not None and state.root is root
                assert state.savepoint.parent is outer
                b.configure("optional-success")
                with conflict_savepoint(b.db):
                    inner = current_conflict_savepoint(b.db)
                    assert (
                        inner is not None and inner.savepoint.parent is state.savepoint
                    )
                    b.configure("inner-success")
                assert current_conflict_savepoint(b.db) is state
                b.preserved(root, state.savepoint)
                # Root-only fence admission is unchanged within Kernel too.
                with pytest.raises(OwnerCommandError, match="exact active owner root"):
                    current_owner_root_transaction(b.db)
                with pytest.raises(OwnerCommandError) as nested_product:
                    execute_owner_savepoint(b.db, lambda: None)
                assert nested_product.value.code == BOUNDARY_CODE
            b.preserved(root, outer)
            assert current_conflict_savepoint(b.db) is None
            with pytest.raises(IntegrityError):
                with conflict_savepoint(b.db):
                    b.pending("root")
                    b.db.flush()
            b.preserved(root, outer)
            raise original

        with pytest.raises(ValueError) as caught:
            execute_owner_savepoint(b.db, optional)
        assert caught.value is original
        b.preserved(root)
        assert current_owner_root_transaction(b.db) is root
        assert b.visible() == set()
        b.configure("durable-optional-failure-evidence")

    b.run(operation)
    assert b.visible() == {
        b.prefix + "root",
        b.prefix + "durable-optional-failure-evidence",
    }
    b.clean()


@pytest.mark.parametrize(
    "failure_event", ["after_soft_rollback", "after_transaction_end", "after_commit"]
)
@pytest.mark.parametrize("swallow", [True, False])
def test_kernel_cleanup_failure_latch_survives_helper_and_optional_cleanup(
    kernel_boundary: KernelBoundary,
    failure_event: str,
    swallow: bool,
) -> None:
    b = kernel_boundary
    first = ValueError("first Kernel body failure")
    cause = LookupError("original diagnostic cause")
    cleanup = RuntimeError("Kernel cleanup listener failure")
    fired = False

    def fail(db: Session, *args: SessionTransaction) -> None:
        nonlocal fired
        state = current_conflict_savepoint(db)
        if db is not b.db or fired or state is None:
            return
        if failure_event == "after_commit":
            assert state.phase is ConflictSavepointPhase.RELEASE
            fired = True
            raise first from cause
        if args[0] is state.savepoint:
            fired = True
            assert state.phase is ConflictSavepointPhase.ROLLBACK
            raise cleanup

    def operation() -> None:
        root = current_owner_root_transaction(b.db)
        b.configure("root")

        def optional() -> None:
            enclosing = b.db.get_nested_transaction()
            assert enclosing is not None
            try:
                with conflict_savepoint(b.db):
                    b.configure("child")
                    if failure_event != "after_commit":
                        raise first from cause
            except ValueError as captured:
                assert captured is first and captured.__cause__ is cause
                assert current_conflict_savepoint(b.db) is None
                assert conflict_savepoint_requires_rollback(b.db, expected_root=root)
                # The Kernel child was detached; successful enclosing cleanup
                # must not remove the exact-root latch or permit later commit.
                assert b.db.get_nested_transaction() is enclosing
                raise

        try:
            execute_owner_savepoint(b.db, optional)
        except ValueError as captured:
            assert captured is first and captured.__cause__ is cause
            assert conflict_savepoint_requires_rollback(b.db, expected_root=root)
            assert b.db.get_transaction() is root and root.is_active
            assert not b.db.in_nested_transaction()
            if not swallow:
                raise
        assert not claim_conflict_savepoint_commit(b.db, expected_root=root)
        with pytest.raises((ConflictSavepointProtocolError, OwnerCommandError)):
            root.commit()
        assert root.is_active and b.db.get_transaction() is root
        assert conflict_savepoint_requires_rollback(b.db, expected_root=root)
        # Admission of any later optional stage also consults Kernel's latch.
        with pytest.raises(OwnerCommandError) as refused:
            execute_owner_savepoint(b.db, lambda: b.configure("forbidden"))
        assert refused.value.code == BOUNDARY_CODE
        assert conflict_savepoint_requires_rollback(b.db, expected_root=root)
        assert b.visible() == set()

    event.listen(Session, failure_event, fail)
    try:
        with pytest.raises(OwnerCommandError if swallow else ValueError) as captured:
            b.run(operation)
        if not swallow:
            assert captured.value is first and captured.value.__cause__ is cause
    finally:
        event.remove(Session, failure_event, fail)
    assert fired and first.__cause__ is cause and b.visible() == set()
    b.clean()
    b.run(lambda: b.configure("fresh"))
    b.clean()


def test_direct_login_rls_filter_and_with_check_survive_conflict_and_clean_root_reuse(
    kernel_boundary: KernelBoundary,
) -> None:
    b = kernel_boundary

    def operation() -> None:
        root = current_owner_root_transaction(b.db)
        b.configure("root")
        b.db.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(b.other_tenant)},
        )
        assert (
            b.db.scalars(
                select(NumberSeries).where(
                    NumberSeries.series_code == b.prefix + "root"
                )
            ).all()
            == []
        )
        changed = b.db.execute(
            text(
                "UPDATE mod_numbering.number_series SET start_value=99 WHERE series_code=:code"
            ),
            {"code": b.prefix + "root"},
        )
        assert changed.rowcount == 0
        with pytest.raises(DBAPIError):
            with conflict_savepoint(b.db):
                b.pending("wrong-tenant")
                b.db.flush()
        assert b.db.get_transaction() is root and root.is_active
        assert b.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(
            b.other_tenant
        )
        assert current_conflict_savepoint(b.db) is None
        assert not conflict_savepoint_requires_rollback(b.db, expected_root=root)
        assert not claim_conflict_savepoint_commit(b.db, expected_root=root)
        b.db.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(OPERATOR_TENANT_ID)},
        )
        b.preserved(root)
        b.configure("evidence")
        assert b.visible() == set()

    b.run(operation)
    assert b.visible() == {b.prefix + "root", b.prefix + "evidence"}
    b.clean()

    def fresh() -> None:
        root = current_owner_root_transaction(b.db)
        assert not claim_conflict_savepoint_commit(b.db, expected_root=root)
        with conflict_savepoint(b.db):
            b.configure("fresh")
        b.preserved(root)
        assert not claim_conflict_savepoint_commit(b.db, expected_root=root)

    b.run(fresh)
    assert b.visible() == {
        b.prefix + suffix for suffix in ("root", "evidence", "fresh")
    }
    b.clean()
