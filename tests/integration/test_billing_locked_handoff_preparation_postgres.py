"""Hosted component evidence on existing isolated real-migration PG16 clones.

The imported final-owner registration exists only in tests. These canaries do
not install or qualify the genuine global handoff owner or production roles.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.services.billing import legacy_handoff_preparation as preparation
from app.services.billing.legacy_archive_fence import ArchiveFenceRefused
from app.services.billing.legacy_archive_fence_contracts import (
    ArchiveFenceRequest,
    ArchiveFenceStagedObservation,
)
from app.services.billing.legacy_handoff_preparation import LockedLegacyHandoffRefused
from app.services.billing.legacy_handoff_preparation_contracts import (
    LockedLegacyHandoffStagedObservation,
    PrepareLockedLegacyHandoffRequest,
)
from app.services.billing.legacy_retained_columns_contracts import (
    RetainedColumnsRequest,
    RetainedColumnsStagedObservation,
)
from app.services.billing.opening_export import (
    BillingOpeningExportError,
    billing_opening_lock_inventory,
    capture_locked_billing_opening_population,
)
from app.services.billing.opening_export_contracts import (
    BillingOpeningLockInventoryQuery,
    CaptureLockedBillingOpeningPopulationQuery,
    LockedBillingOpeningPopulation,
    PreviewBillingOpeningExportQuery,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext, execute_owner_command
from tests.integration.test_billing_legacy_archive_fence_postgres import (
    _DEFINITION,
    FenceConnections,
    fence_connections,  # noqa: F401 -- shared isolated actual-login fixture
)
from tests.integration.test_billing_legacy_privilege_closure_postgres import (
    _acl,
    _no_install,
)
from tests.integration.test_billing_legacy_retained_columns_postgres import (
    _subscriber,
    retained_connections,  # noqa: F401 -- shared migrated clone and ownership
)
from tests.integration.test_billing_runtime_login_postgres import (
    runtime_connections,  # noqa: F401 -- transitive real migration-chain fixture
)

pytestmark = pytest.mark.integration


@pytest.fixture
def preparation_connections(
    retained_connections: FenceConnections,  # noqa: F811 -- imported fixture
) -> Iterator[FenceConnections]:
    connections = retained_connections
    # Private clone provisioning only; derive the extra inventory from the
    # existing public v3 collector rather than maintaining another table list.
    with Session(connections.owner) as db, db.begin():
        census = billing_opening_lock_inventory(
            db, query=BillingOpeningLockInventoryQuery()
        )
    for name in census.relations:
        connections.admin.execute(
            sql.SQL("ALTER TABLE public.{} OWNER TO {}").format(
                sql.Identifier(name), sql.Identifier(connections.owner.url.username)
            )
        )
    connections.admin.execute(
        sql.SQL("GRANT SELECT ON public.resellers TO {}").format(
            sql.Identifier(connections.owner.url.username)
        )
    )
    yield connections


def _request() -> PrepareLockedLegacyHandoffRequest:
    return PrepareLockedLegacyHandoffRequest(
        source=PreviewBillingOpeningExportQuery(
            source_repository_sha="c28086e2c1361b2126964c5d1ff1eb9eddf29021",
            installation_id=uuid4(),
            economic_cutoff=datetime.now(UTC) - timedelta(seconds=1),
            currency_precisions=(("NGN", 2),),
            population_version="sub-billing-opening-census-v3",
        )
    )


def _context() -> CommandContext:
    return CommandContext.system(
        actor="hosted-locked-component-canary",
        scope="private-real-migrated-clone",
        reason="locked preparation component evidence only",
        idempotency_key="locked-component-v1",
    )


def _stage(
    db: Session, request: PrepareLockedLegacyHandoffRequest
) -> LockedLegacyHandoffStagedObservation:
    db.execute(text("SET LOCAL lock_timeout='2s'"))
    db.execute(text("SET LOCAL statement_timeout='30s'"))
    return preparation._stage_locked_legacy_handoff(db, request=request)


def _install(
    connections: FenceConnections, request: PrepareLockedLegacyHandoffRequest
) -> LockedLegacyHandoffStagedObservation:
    with Session(connections.owner) as db:
        return execute_owner_command(
            db,
            definition=_DEFINITION,
            context=_context(),
            operation=lambda: _stage(db, request),
        )


def _audit_count(connections: FenceConnections) -> int:
    row = connections.admin.execute("""SELECT count(*) FROM event_store
        WHERE event_type IN ('billing.legacy_archive_fence.staged',
                            'billing.legacy_retained_columns.staged')""").fetchone()
    assert row is not None
    return int(row[0])


def test_complete_54_union_precedes_first_capture_and_all_effects_and_replay(
    preparation_connections: FenceConnections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = preparation_connections
    request = _request()
    statements: list[str] = []
    original = capture_locked_billing_opening_population
    captures: list[LockedBillingOpeningPopulation] = []

    def observe(
        db: Session, *, query: CaptureLockedBillingOpeningPopulationQuery
    ) -> LockedBillingOpeningPopulation:
        locks = (
            db.execute(
                text("""SELECT c.relname FROM pg_locks l
            JOIN pg_class c ON c.oid=l.relation
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE l.pid=pg_backend_pid() AND l.granted
              AND l.mode='AccessExclusiveLock' AND n.nspname='public'
            ORDER BY c.relname""")
            )
            .scalars()
            .all()
        )
        assert len(locks) == 54
        assert locks == sorted(preparation._inventory(db))
        if not captures:
            assert len([s for s in statements if s.startswith("LOCK TABLE ")]) == 1
            assert not any(
                s.startswith(
                    (
                        "REVOKE ",
                        "CREATE FUNCTION ",
                        "CREATE TRIGGER ",
                        "INSERT INTO event_store",
                    )
                )
                for s in statements
            )
        result = original(db, query=query)
        captures.append(result)
        return result

    def sql_observer(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    monkeypatch.setattr(
        preparation, "capture_locked_billing_opening_population", observe
    )
    event.listen(connections.owner, "before_cursor_execute", sql_observer)
    try:
        first = _install(connections, request)
    finally:
        event.remove(connections.owner, "before_cursor_execute", sql_observer)
    assert len(first.relations) == 54
    assert len(captures) == 2 and preparation._same_census(*captures)
    assert not first.archive.replay_observed and not first.retained.replay_observed
    assert _audit_count(connections) == 2
    monkeypatch.setattr(
        preparation, "capture_locked_billing_opening_population", original
    )
    replay = _install(connections, request)
    assert replay.archive.replay_observed and replay.retained.replay_observed
    assert replay.archive.catalog_digest == first.archive.catalog_digest
    assert replay.retained.catalog_digest == first.retained.catalog_digest
    assert replay.relations == first.relations
    assert _audit_count(connections) == 2
    # Actual runtime privilege/guard denial and ordinary retained lifecycle.
    runtime = connections.runtime
    runtime.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        runtime.execute("UPDATE public.invoices SET id=id WHERE false")
    runtime.rollback()
    subscriber = _subscriber(runtime, None)
    runtime.execute(
        "UPDATE subscribers SET first_name='Still writable' WHERE id=%s", (subscriber,)
    )
    runtime.commit()


def test_legacy_writer_wait_is_observed_and_committed_row_enters_fresh_capture(
    preparation_connections: FenceConnections,
) -> None:
    connections = preparation_connections
    runtime = connections.runtime
    runtime.rollback()
    subscriber = _subscriber(runtime, None)
    writer_pid = runtime.info.backend_pid
    lock_started = Event()
    owner_pid: list[int] = []

    def attempt() -> LockedLegacyHandoffStagedObservation:
        with Session(connections.owner) as db:

            def operation() -> LockedLegacyHandoffStagedObservation:
                owner_pid.append(
                    db.execute(text("SELECT pg_backend_pid()")).scalar_one()
                )
                lock_started.set()
                return _stage(db, _request())

            return execute_owner_command(
                db, definition=_DEFINITION, context=_context(), operation=operation
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(attempt)
        try:
            assert lock_started.wait(5)
            deadline = monotonic() + 1.5
            witnessed = False
            while monotonic() < deadline:
                row = connections.admin.execute(
                    "SELECT %s=ANY(pg_blocking_pids(%s))", (writer_pid, owner_pid[0])
                ).fetchone()
                if row == (True,):
                    witnessed = True
                    break
                sleep(0.01)
            assert witnessed, (
                "No actual blocking witness; elapsed time alone is insufficient."
            )
            runtime.commit()
            observation = future.result(timeout=35)
            assert subscriber in observation.capture.population.subscriber_ids
        finally:
            runtime.rollback()


@pytest.mark.parametrize(
    "component",
    ("_install_legacy_archive_fence", "_install_legacy_retained_columns_fence"),
)
def test_failure_after_each_actual_component_rolls_back_acls_guards_and_audits(
    preparation_connections: FenceConnections,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    connections = preparation_connections
    before = _acl(connections.admin)
    archive_original = preparation._install_legacy_archive_fence
    retained_original = preparation._install_legacy_retained_columns_fence

    def fail_archive(
        db: Session, *, request: ArchiveFenceRequest
    ) -> ArchiveFenceStagedObservation:
        archive_original(db, request=request)
        raise RuntimeError("injected after actual component")

    def fail_retained(
        db: Session, *, request: RetainedColumnsRequest
    ) -> RetainedColumnsStagedObservation:
        retained_original(db, request=request)
        raise RuntimeError("injected after actual component")

    if component == "_install_legacy_archive_fence":
        monkeypatch.setattr(preparation, component, fail_archive)
    else:
        monkeypatch.setattr(preparation, component, fail_retained)
    with pytest.raises(RuntimeError, match="injected after actual component"):
        _install(connections, _request())
    assert _acl(connections.admin) == before
    _no_install(connections.admin)


def test_census_only_lock_timeout_has_zero_component_effects(
    preparation_connections: FenceConnections,
) -> None:
    connections = preparation_connections
    before = _acl(connections.admin)
    runtime = connections.runtime
    runtime.rollback()
    runtime.execute("LOCK TABLE public.topup_intents IN ACCESS SHARE MODE")
    try:
        with pytest.raises(OperationalError, match="lock timeout"):
            _install(connections, _request())
    finally:
        runtime.rollback()
    assert _acl(connections.admin) == before
    _no_install(connections.admin)


@pytest.mark.parametrize(
    "mode",
    ("temp", "repeatable_read", "savepoint", "wrong_mapper_bind", "connection_bound"),
)
def test_wrong_actual_root_or_relation_refuses_without_effects(
    preparation_connections: FenceConnections,
    mode: str,
) -> None:
    connections = preparation_connections
    engine = (
        connections.owner.execution_options(isolation_level="REPEATABLE READ")
        if mode == "repeatable_read"
        else connections.owner
    )
    with connections.owner.connect() as external:
        bind = external if mode == "connection_bound" else engine
        with Session(
            bind,
            binds={Invoice: connections.owner.execution_options()}
            if mode == "wrong_mapper_bind"
            else None,
        ) as db:

            def operation() -> LockedLegacyHandoffStagedObservation:
                if mode == "temp":
                    db.execute(text("CREATE TEMP TABLE subscribers (id uuid)"))
                if mode == "savepoint":
                    db.begin_nested()
                return _stage(db, _request())

            with pytest.raises(DomainError):
                execute_owner_command(
                    db, definition=_DEFINITION, context=_context(), operation=operation
                )
    _no_install(connections.admin)


def test_public_census_refuses_missing_actual_locks(
    preparation_connections: FenceConnections,
) -> None:
    with Session(preparation_connections.owner) as db:
        with pytest.raises(BillingOpeningExportError, match="Every census relation"):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=_context(),
                operation=lambda: capture_locked_billing_opening_population(
                    db,
                    query=CaptureLockedBillingOpeningPopulationQuery(_request().source),
                ),
            )
    _no_install(preparation_connections.admin)


def test_actual_locked_semantic_mutation_refuses_and_owner_rolls_back(
    preparation_connections: FenceConnections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = preparation_connections
    before = _acl(connections.admin)
    original = preparation._install_legacy_retained_columns_fence

    def mutate_after(
        db: Session, *, request: RetainedColumnsRequest
    ) -> RetainedColumnsStagedObservation:
        result = original(db, request=request)
        # Actual product row insertion is permitted by retained policy but
        # changes the captured population inside this root and must abort it.
        db.connection().exec_driver_sql(
            """INSERT INTO subscribers
            (id,first_name,last_name,email,email_verified,gender,status,user_type,is_active,
             marketing_opt_in,reseller_id,billing_enabled,captive_redirect_enabled,billing_mode,created_at,updated_at)
            VALUES (%s,'Drift','Canary','drift@example.invalid',false,'unknown','new','customer',true,
              false,(SELECT id FROM resellers WHERE is_house),true,false,'postpaid',now(),now())""",
            (uuid4(),),
        )
        return result

    monkeypatch.setattr(
        preparation, "_install_legacy_retained_columns_fence", mutate_after
    )
    with pytest.raises(LockedLegacyHandoffRefused, match="semantic census"):
        _install(connections, _request())
    assert _acl(connections.admin) == before
    _no_install(connections.admin)


def test_actual_archive_guard_drift_after_retained_stage_refuses_final_reobservation(
    preparation_connections: FenceConnections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = preparation_connections
    before_acl = _acl(connections.admin)
    original_retained = preparation._install_legacy_retained_columns_fence
    captures: list[LockedBillingOpeningPopulation] = []

    def observe_capture(
        db: Session, *, query: CaptureLockedBillingOpeningPopulationQuery
    ) -> LockedBillingOpeningPopulation:
        result = capture_locked_billing_opening_population(db, query=query)
        captures.append(result)
        return result

    def disable_actual_guard_after_retained(
        db: Session, *, request: RetainedColumnsRequest
    ) -> RetainedColumnsStagedObservation:
        result = original_retained(db, request=request)
        assert len(captures) == 1
        assert (
            db.execute(
                text("""SELECT count(*) FROM public.event_store
                WHERE event_type IN ('billing.legacy_archive_fence.staged',
                                     'billing.legacy_retained_columns.staged')""")
            ).scalar_one()
            == 2
        )
        # Change the actual installed catalog after both real components,
        # without changing any census data or replacing their observations.
        db.execute(
            text("""ALTER TABLE public.invoices
                DISABLE TRIGGER dotmac_billing_archive_row_v1""")
        )
        assert (
            db.execute(
                text("""SELECT tgenabled FROM pg_catalog.pg_trigger
                WHERE tgrelid='public.invoices'::regclass
                  AND tgname='dotmac_billing_archive_row_v1'""")
            ).scalar_one()
            == "D"
        )
        return result

    monkeypatch.setattr(
        preparation, "capture_locked_billing_opening_population", observe_capture
    )
    monkeypatch.setattr(
        preparation,
        "_install_legacy_retained_columns_fence",
        disable_actual_guard_after_retained,
    )
    with pytest.raises(ArchiveFenceRefused, match="enabled state drift"):
        _install(connections, _request())
    # Both captures really ran and their semantics matched: refusal therefore
    # came from final actual fence reobservation, not census drift detection.
    assert len(captures) == 2
    assert preparation._same_census(*captures)
    assert _acl(connections.admin) == before_acl
    _no_install(connections.admin)


@pytest.mark.parametrize(
    "version", ("sub-billing-opening-census-v1", "sub-billing-opening-census-v2")
)
def test_older_census_never_stages_components(
    preparation_connections: FenceConnections,
    version: str,
) -> None:
    request = _request()
    with pytest.raises(LockedLegacyHandoffRefused, match="complete v3"):
        _install(
            preparation_connections,
            replace(
                request, source=replace(request.source, population_version=version)
            ),
        )
    _no_install(preparation_connections.admin)


def test_participant_requires_actual_final_owner_executor(
    preparation_connections: FenceConnections,
) -> None:
    with Session(preparation_connections.owner) as db, db.begin():
        with pytest.raises(ArchiveFenceRefused, match="registered handoff owner"):
            _stage(db, _request())
    _no_install(preparation_connections.admin)


def test_comparison_preserves_actual_census_cutoff_and_migration_heads(
    preparation_connections: FenceConnections,
) -> None:
    observed = _install(preparation_connections, _request()).capture
    relabeled = replace(
        observed,
        capture=replace(
            observed.capture,
            captured_at=observed.capture.captured_at + timedelta(seconds=1),
            snapshot_id="different observation label",
        ),
    )
    assert preparation._same_census(observed, relabeled)
    for changed in (
        replace(observed, capture=replace(observed.capture, schema_heads=("changed",))),
        replace(
            observed,
            capture=replace(
                observed.capture,
                declared_source=replace(
                    observed.capture.declared_source,
                    economic_cutoff=observed.capture.declared_source.economic_cutoff
                    - timedelta(seconds=1),
                ),
            ),
        ),
        replace(
            observed,
            population=replace(
                observed.population,
                subscriber_ids=(*observed.population.subscriber_ids, uuid4()),
            ),
        ),
        replace(observed, cash_evidence=None),
        replace(observed, documentary_observations=None),
    ):
        assert not preparation._same_census(observed, changed)
