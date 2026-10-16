"""Private archive canaries on the migrated isolated hosted PostgreSQL service.

The fixture supplies a test-only future owner contract and transfers ONLY its
private clone's archive/schema ownership to an ephemeral non-superuser login.
This is component acceptance, never evidence of production role provisioning.
RuntimeConnections authenticates app_user directly and restores its proven NULL
password under the existing cluster-wide maintenance lock. No SET ROLE login.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services.billing.legacy_archive_fence import (
    ArchiveFenceRefused,
    _install_legacy_archive_fence,
)
from app.services.billing.legacy_archive_fence_contracts import (
    ArchiveFenceRequest,
    ArchiveFenceStagedObservation,
    ArchiveRelation,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    ConcernContract,
    OwnerRole,
    TransactionMode,
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.registry import service_relationship
from tests.integration.test_billing_runtime_login_postgres import (
    RuntimeConnections,
    runtime_connections,  # noqa: F401 -- shared isolated actual-login fixture
)

pytestmark = pytest.mark.integration
_DEFINITION = OwnerCommandDefinition(
    owner="runtime.billing_ledger_handoff",
    concern="installation-wide Billing ledger handoff",
    name="install_billing_ledger_handoff",
)


@dataclass(frozen=True)
class FenceConnections:
    owner: Engine
    admin: psycopg.Connection
    runtime: psycopg.Connection


def _future_owner_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test declaration only; use the real executor and manifest validation."""
    from app.services import sot_relationships
    from app.services.owner_commands import _validate_manifest

    base = service_relationship("runtime.billing_runtime_login")
    assert base.contract is not None
    owner = replace(
        base,
        name=_DEFINITION.owner,
        module="app.services.billing.ledger_handoff",
        owns=(_DEFINITION.concern,),
        contract=replace(
            base.contract,
            concerns=(
                ConcernContract(
                    _DEFINITION.concern, OwnerRole.APPLICATION_COORDINATOR, ("catalog",)
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    "catalog",
                    "external:postgresql_catalog",
                    AuthorityKind.EXTERNAL_OBSERVATION,
                    "Test-only migrated clone catalog",
                ),
            ),
            transaction=replace(
                base.contract.transaction, mode=TransactionMode.COORDINATOR_MANAGED
            ),
            errors=replace(
                base.contract.errors,
                domain_codes=owner_command_boundary_error_codes(_DEFINITION.owner),
            ),
            migration=replace(base.contract.migration, new_owner=_DEFINITION.owner),
        ),
    )
    from app.services.billing import legacy_archive_fence

    monkeypatch.setattr(
        legacy_archive_fence,
        "service_relationship",
        lambda name: owner if name == owner.name else service_relationship(name),
    )
    original = sot_relationships.service_relationship
    monkeypatch.setattr(
        sot_relationships,
        "service_relationship",
        lambda name: owner if name == owner.name else original(name),
    )
    all_original = sot_relationships.all_services
    monkeypatch.setattr(
        sot_relationships, "all_services", lambda: (*all_original(), owner)
    )
    _validate_manifest.cache_clear()


@pytest.fixture
def fence_connections(
    runtime_connections: RuntimeConnections,  # noqa: F811 -- imported fixture parameter
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FenceConnections]:
    _future_owner_contract(monkeypatch)
    from app.services.events import dispatcher

    # Accept the real transactional pending event row; no hosted test may send
    # generic delivery callbacks to product transports after its owner commits.
    monkeypatch.setattr(dispatcher, "run_after_commit", lambda db, callback: None)
    admin = runtime_connections.admin
    role = "archive_owner_" + uuid4().hex
    password = uuid4().hex
    admin.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password))
    )
    engine: Engine | None = None
    try:
        # Private disposable clone only. Never alter shared template ownership.
        admin.execute(
            sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(role))
        )
        for relation in ArchiveRelation:
            admin.execute(
                sql.SQL("ALTER TABLE public.{} OWNER TO {}").format(
                    sql.Identifier(relation.value), sql.Identifier(role)
                )
            )
        engine = create_engine(
            URL.create(
                "postgresql+psycopg",
                username=role,
                password=password,
                host=admin.info.host,
                port=admin.info.port,
                database=admin.info.dbname,
            ),
            isolation_level="READ COMMITTED",
        )
        admin.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON public.event_store TO {}").format(
                sql.Identifier(role)
            )
        )
        yield FenceConnections(engine, admin, runtime_connections.runtime)
    finally:
        if engine is not None:
            engine.dispose()
        # All owned objects belong to this private clone. REASSIGN retains its
        # data for the clone fixture's normal teardown; no DROP OWNED CASCADE.
        admin.execute(
            sql.SQL("REASSIGN OWNED BY {} TO postgres").format(sql.Identifier(role))
        )
        admin.execute(sql.SQL("DROP OWNED BY {} RESTRICT").format(sql.Identifier(role)))
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        from app.services.owner_commands import _validate_manifest

        _validate_manifest.cache_clear()


def _stage(db: Session) -> ArchiveFenceStagedObservation:
    db.execute(text("SET LOCAL lock_timeout = '2s'"))
    db.execute(text("SET LOCAL statement_timeout = '30s'"))
    return _install_legacy_archive_fence(db, request=ArchiveFenceRequest())


def _install(engine: Engine) -> ArchiveFenceStagedObservation:
    with Session(engine) as db:
        return execute_owner_command(
            db,
            definition=_DEFINITION,
            context=CommandContext.system(
                actor="hosted-component-canary",
                scope="private-migrated-clone",
                reason="archive component acceptance",
                idempotency_key="archive-component-v1",
            ),
            operation=lambda: _stage(db),
        )


def _denied(
    runtime: psycopg.Connection, statement: sql.Composed | str, *, guard: bool = True
) -> None:
    # Each attempted statement has its own actual authenticated transaction.
    runtime.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
        runtime.execute(statement)
    if guard:
        assert "legacy monetary archive is immutable" in str(refused.value)
    runtime.rollback()


def _excluded_catalog(admin: psycopg.Connection) -> list[tuple[object, ...]]:
    return admin.execute(
        """
        SELECT c.oid, n.nspname, c.relname, c.relowner, c.relacl::text,
               COALESCE((SELECT string_agg(att.attnum::text || ':' || att.attacl::text, ',' ORDER BY att.attnum) FROM pg_attribute att WHERE att.attrelid=c.oid AND att.attacl IS NOT NULL), ''),
               COALESCE((SELECT string_agg(t.oid::text || ':' || pg_get_triggerdef(t.oid) || ':' || t.tgenabled, ',' ORDER BY t.oid) FROM pg_trigger t WHERE t.tgrelid=c.oid), '')
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname NOT LIKE 'pg_%%' AND n.nspname <> 'information_schema'
           AND c.relkind IN ('r','p')
           AND NOT (n.nspname='public' AND c.relname = ANY(%s)) ORDER BY c.oid
    """,
        ([relation.value for relation in ArchiveRelation],),
    ).fetchall()


def test_all_44_operations_replay_and_excluded_catalog(
    fence_connections: FenceConnections,
) -> None:
    admin, runtime = fence_connections.admin, fence_connections.runtime
    excluded = _excluded_catalog(admin)
    existing_guards = admin.execute(
        "SELECT t.oid, pg_get_triggerdef(t.oid), t.tgenabled, t.tgfoid FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname = ANY(%s) ORDER BY t.oid",
        ([r.value for r in ArchiveRelation],),
    ).fetchall()
    # Exercise explicit column grant options and PUBLIC DML removal, not just
    # migration-default privileges. Preserve SELECT for actual runtime reads.
    admin.execute(
        "GRANT INSERT (id), UPDATE (id) ON public.invoices TO app_user WITH GRANT OPTION"
    )
    admin.execute("GRANT UPDATE (id) ON public.invoices TO PUBLIC")
    admin.execute("GRANT INSERT, UPDATE, DELETE, TRUNCATE ON public.invoices TO PUBLIC")
    first = _install(fence_connections.owner)
    second = _install(fence_connections.owner)
    assert first.replay_observed is False
    assert second.replay_observed is True
    assert first.catalog_digest == second.catalog_digest
    assert len(first.relations) == 44
    assert admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_archive_fence.staged'"
    ).fetchone() == (1,)
    assert excluded == _excluded_catalog(admin)
    assert (
        existing_guards
        == admin.execute(
            "SELECT t.oid, pg_get_triggerdef(t.oid), t.tgenabled, t.tgfoid FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname = ANY(%s) AND t.tgname NOT IN ('dotmac_billing_archive_row_v1', 'dotmac_billing_archive_statement_v1') ORDER BY t.oid",
            ([r.value for r in ArchiveRelation],),
        ).fetchall()
    )
    assert runtime.execute("SELECT session_user, current_user").fetchone() == (
        "app_user",
        "app_user",
    )
    runtime.rollback()
    # TRUNCATE CASCADE checks privileges on every reached relation before
    # invoking guards. Grant TRUNCATE on the clone's public tables so archive
    # trigger assertions cannot succeed through unrelated cascade ACL denial.
    for (name,) in admin.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p')"
    ).fetchall():
        admin.execute(
            sql.SQL("GRANT TRUNCATE ON public.{} TO app_user").format(
                sql.Identifier(name)
            )
        )
    for relation in ArchiveRelation:
        admin.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public.{} TO app_user"
            ).format(sql.Identifier(relation.value))
        )
    for relation in ArchiveRelation:
        table = sql.Identifier("public", relation.value)
        # Grant effective DML after installation ONLY as a controlled trigger
        # canary. Every operation must hit an unconditional statement guard,
        # including empty UPDATE/DELETE/INSERT and TRUNCATE on empty tables.
        admin.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {} TO app_user"
            ).format(table)
        )
        assert (
            runtime.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()
            is not None
        )
        runtime.rollback()
        column = admin.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum LIMIT 1",
            ("public." + relation.value,),
        ).fetchone()[0]
        for statement in (
            sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(table, table),
            sql.SQL("UPDATE {} SET {} = {} WHERE false").format(
                table, sql.Identifier(column), sql.Identifier(column)
            ),
            sql.SQL("DELETE FROM {} WHERE false").format(table),
            sql.SQL("TRUNCATE {} CASCADE").format(table),
        ):
            _denied(runtime, statement)
        _denied(
            runtime,
            sql.SQL("ALTER TABLE {} DISABLE TRIGGER ALL").format(table),
            guard=False,
        )
    for relation in ArchiveRelation:
        table = sql.Identifier("public", relation.value)
        admin.execute(
            sql.SQL(
                "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {} FROM app_user RESTRICT"
            ).format(table)
        )
    # Retained product operation still reaches its own unchanged contract.
    runtime.execute("UPDATE public.subscribers SET first_name=first_name WHERE false")
    runtime.rollback()
    assert _install(fence_connections.owner).replay_observed is True


def test_rollback_and_guard_drift_refuse(fence_connections: FenceConnections) -> None:
    engine, admin = fence_connections.owner, fence_connections.admin

    class InjectedFailure(Exception):
        pass

    with Session(engine) as db:

        def fail_after_staging() -> None:
            _stage(db)
            raise InjectedFailure

        with pytest.raises(InjectedFailure):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary",
                    scope="clone",
                    reason="atomic rollback",
                    idempotency_key="archive-rollback-v1",
                ),
                operation=fail_after_staging,
            )
    assert admin.execute(
        "SELECT to_regprocedure('public.dotmac_billing_archive_deny_v1()') IS NULL"
    ).fetchone() == (True,)
    assert admin.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'dotmac_billing_archive_%'"
    ).fetchone() == (0,)
    assert admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_archive_fence.staged'"
    ).fetchone() == (0,)
    _install(engine)
    admin.execute(
        "ALTER TABLE public.invoices DISABLE TRIGGER dotmac_billing_archive_row_v1"
    )
    with pytest.raises(ArchiveFenceRefused):
        _install(engine)
    admin.execute(
        "ALTER TABLE public.invoices ENABLE ALWAYS TRIGGER dotmac_billing_archive_row_v1"
    )
    admin.execute("GRANT UPDATE(id) ON public.invoices TO app_user")
    with pytest.raises(ArchiveFenceRefused):
        _install(engine)


def test_wrong_role_nested_external_isolation_and_dirty_refuse(
    fence_connections: FenceConnections,
) -> None:
    engine = fence_connections.owner
    with Session(engine) as db:
        with pytest.raises(ArchiveFenceRefused):
            _install_legacy_archive_fence(db, request=ArchiveFenceRequest())
    for change in (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "SET TRANSACTION READ ONLY",
    ):
        with Session(engine) as db:

            def invalid_posture(change: str = change) -> object:
                db.execute(text(change))
                return _stage(db)

            with pytest.raises(ArchiveFenceRefused):
                execute_owner_command(
                    db,
                    definition=_DEFINITION,
                    context=CommandContext.system(
                        actor="canary", scope="clone", reason="posture refusal"
                    ),
                    operation=invalid_posture,
                )
    with engine.connect() as connection, connection.begin(), Session(connection) as db:
        with pytest.raises(ArchiveFenceRefused):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary", scope="clone", reason="external root refusal"
                ),
                operation=lambda: _stage(db),
            )
    with Session(engine) as db:

        def nested() -> object:
            with db.begin_nested():
                return _stage(db)

        with pytest.raises(ArchiveFenceRefused):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary", scope="clone", reason="nested refusal"
                ),
                operation=nested,
            )
    with Session(engine) as db:

        def dirty() -> object:
            db.add(Subscriber(first_name="Pending", last_name="Canary"))
            return _stage(db)

        with pytest.raises(ArchiveFenceRefused):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary", scope="clone", reason="dirty refusal"
                ),
                operation=dirty,
            )
    # Actual wrong current role: postgres owns neither archive nor public now
    # and is a superuser; no role-name parameter can exempt it.
    admin = fence_connections.admin
    url = URL.create(
        "postgresql+psycopg",
        username=admin.info.user,
        host=admin.info.host,
        port=admin.info.port,
        database=admin.info.dbname,
    )
    # Reuse actual admin DBAPI connection without opening/impersonating a login.
    wrong = create_engine(url, creator=lambda: psycopg.connect(admin.info.dsn))
    try:
        with pytest.raises(ArchiveFenceRefused):
            _install(wrong)
    finally:
        wrong.dispose()


@pytest.mark.parametrize(
    "shape",
    ("missing", "view", "partition_parent", "partition_child", "inheritance", "shadow"),
)
def test_archive_shape_refuses_without_partial_state(
    fence_connections: FenceConnections, shape: str
) -> None:
    admin, engine = fence_connections.admin, fence_connections.owner
    owner = sql.Identifier(engine.url.username)
    renamed = shape in {"missing", "view", "partition_parent", "partition_child"}
    if renamed:
        admin.execute("ALTER TABLE public.invoices RENAME TO archive_shape_original")
    try:
        if shape == "view":
            admin.execute(
                "CREATE VIEW public.invoices AS SELECT * FROM public.archive_shape_original"
            )
            admin.execute(
                sql.SQL("ALTER VIEW public.invoices OWNER TO {}").format(owner)
            )
        elif shape == "partition_parent":
            admin.execute(
                "CREATE TABLE public.invoices (id uuid) PARTITION BY HASH(id)"
            )
            admin.execute(
                sql.SQL("ALTER TABLE public.invoices OWNER TO {}").format(owner)
            )
        elif shape == "partition_child":
            admin.execute(
                "CREATE TABLE public.archive_shape_parent (id uuid) PARTITION BY HASH(id)"
            )
            admin.execute(
                "CREATE TABLE public.invoices PARTITION OF public.archive_shape_parent FOR VALUES WITH (modulus 1, remainder 0)"
            )
            admin.execute(
                sql.SQL("ALTER TABLE public.invoices OWNER TO {}").format(owner)
            )
        elif shape == "inheritance":
            admin.execute(
                "CREATE TABLE public.archive_shape_child () INHERITS (public.invoices)"
            )
        if shape == "shadow":
            with Session(engine) as db:

                def shadow() -> ArchiveFenceStagedObservation:
                    db.execute(text("CREATE TEMP TABLE invoices (id uuid)"))
                    return _stage(db)

                with pytest.raises(ArchiveFenceRefused):
                    execute_owner_command(
                        db,
                        definition=_DEFINITION,
                        context=CommandContext.system(
                            actor="canary", scope="clone", reason="shadow refusal"
                        ),
                        operation=shadow,
                    )
        else:
            with pytest.raises(ArchiveFenceRefused):
                _install(engine)
        assert admin.execute(
            "SELECT to_regprocedure('public.dotmac_billing_archive_deny_v1()') IS NULL"
        ).fetchone() == (True,)
    finally:
        if shape == "view":
            admin.execute("DROP VIEW public.invoices")
        elif shape in {"partition_parent", "partition_child"}:
            admin.execute("DROP TABLE public.invoices")
        if shape == "partition_child":
            admin.execute("DROP TABLE public.archive_shape_parent")
        if shape == "inheritance":
            admin.execute("DROP TABLE public.archive_shape_child")
        if renamed:
            admin.execute(
                "ALTER TABLE public.archive_shape_original RENAME TO invoices"
            )


def test_runtime_owner_membership_even_dormant_refuses(
    fence_connections: FenceConnections,
) -> None:
    admin, engine = fence_connections.admin, fence_connections.owner
    role = sql.Identifier(engine.url.username)
    admin.execute(
        sql.SQL(
            "GRANT {} TO app_user WITH INHERIT FALSE, SET FALSE, ADMIN FALSE"
        ).format(role)
    )
    try:
        with pytest.raises(ArchiveFenceRefused):
            _install(engine)
    finally:
        admin.execute(sql.SQL("REVOKE {} FROM app_user").format(role))


def test_connection_savepoint_and_audit_multibind_refuse(
    fence_connections: FenceConnections,
) -> None:
    from app.models.event_store import EventStore

    engine = fence_connections.owner
    with Session(engine) as db:

        def external_child() -> object:
            child = db.connection().begin_nested()
            try:
                return _stage(db)
            finally:
                child.rollback()

        with pytest.raises(ArchiveFenceRefused):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary", scope="clone", reason="connection child refusal"
                ),
                operation=external_child,
            )
    other = engine.execution_options(schema_translate_map={None: "public"})
    try:
        with Session(engine, binds={EventStore: other}) as db:
            with pytest.raises(ArchiveFenceRefused):
                execute_owner_command(
                    db,
                    definition=_DEFINITION,
                    context=CommandContext.system(
                        actor="canary", scope="clone", reason="audit bind refusal"
                    ),
                    operation=lambda: _stage(db),
                )
        with Session(other) as db:
            with pytest.raises(ArchiveFenceRefused):
                execute_owner_command(
                    db,
                    definition=_DEFINITION,
                    context=CommandContext.system(
                        actor="canary", scope="clone", reason="translated bind refusal"
                    ),
                    operation=lambda: _stage(db),
                )
    finally:
        other.dispose()


def test_row_and_fk_cascade_guards_are_not_vacuous_acl_denials(
    fence_connections: FenceConnections,
) -> None:
    admin, runtime, engine = (
        fence_connections.admin,
        fence_connections.runtime,
        fence_connections.owner,
    )
    parent_id, run_id = uuid4(), uuid4()
    admin.execute("CREATE TABLE public.archive_retained_parent (id uuid PRIMARY KEY)")
    admin.execute(
        "INSERT INTO public.archive_retained_parent VALUES (%s)", (parent_id,)
    )
    admin.execute(
        "ALTER TABLE public.billing_runs ADD COLUMN archive_canary_parent uuid REFERENCES public.archive_retained_parent(id) ON DELETE CASCADE"
    )
    # Populate real migrated billing_runs, naming every required field rather
    # than substituting a metadata-created archive table.
    insert = """INSERT INTO public.billing_runs
        (id, run_at, launch_kind, status, started_at, subscriptions_scanned,
         subscriptions_billed, invoices_created, lines_created, skipped,
         created_at, archive_canary_parent)
        VALUES (%s, now(), 'scheduled', 'running', now(), 0, 0, 0, 0, 0, now(), %s)"""
    admin.execute(insert, (run_id, parent_id))
    _install(engine)
    admin.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON public.billing_runs TO app_user"
    )
    admin.execute("GRANT SELECT, DELETE ON public.archive_retained_parent TO app_user")
    # Deliberately isolate row guards in this disposable clone. Production
    # installs both levels; a cascade normally bypasses child statement guards.
    admin.execute(
        "ALTER TABLE public.billing_runs DISABLE TRIGGER dotmac_billing_archive_statement_v1"
    )
    _denied(runtime, "UPDATE public.billing_runs SET skipped=skipped+1")
    _denied(runtime, "DELETE FROM public.billing_runs")
    runtime.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
        runtime.execute(insert, (uuid4(), parent_id))
    assert "legacy monetary archive is immutable" in str(refused.value)
    runtime.rollback()
    _denied(runtime, "DELETE FROM public.archive_retained_parent")
    assert admin.execute(
        "SELECT count(*) FROM public.billing_runs WHERE id=%s", (run_id,)
    ).fetchone() == (1,)
    assert admin.execute(
        "SELECT count(*) FROM public.archive_retained_parent WHERE id=%s", (parent_id,)
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "drift", ("function_body", "trigger_arguments", "trigger_events")
)
def test_function_and_trigger_drift_refuse_replay(
    fence_connections: FenceConnections,
    drift: str,
) -> None:
    admin, engine = fence_connections.admin, fence_connections.owner
    _install(engine)
    if drift == "function_body":
        admin.execute("""CREATE OR REPLACE FUNCTION public.dotmac_billing_archive_deny_v1()
            RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path TO pg_catalog
            AS $$ BEGIN RETURN NEW; END; $$""")
    else:
        admin.execute(
            "DROP TRIGGER dotmac_billing_archive_statement_v1 ON public.billing_runs"
        )
        if drift == "trigger_arguments":
            admin.execute(
                "CREATE TRIGGER dotmac_billing_archive_statement_v1 BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.billing_runs FOR EACH STATEMENT EXECUTE FUNCTION public.dotmac_billing_archive_deny_v1('unexpected')"
            )
        else:
            admin.execute(
                "CREATE TRIGGER dotmac_billing_archive_statement_v1 BEFORE UPDATE ON public.billing_runs FOR EACH STATEMENT EXECUTE FUNCTION public.dotmac_billing_archive_deny_v1()"
            )
        admin.execute(
            "ALTER TABLE public.billing_runs ENABLE ALWAYS TRIGGER dotmac_billing_archive_statement_v1"
        )
    with pytest.raises(ArchiveFenceRefused):
        _install(engine)


def test_lock_wait_issues_no_mutation_before_all_44_locks_and_rolls_back(
    fence_connections: FenceConnections,
) -> None:
    from queue import Queue
    from threading import Event, Lock, Thread

    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    admin, engine = fence_connections.admin, fence_connections.owner
    last = sorted(ArchiveRelation, key=lambda relation: relation.value)[-1]
    archive_names = [relation.value for relation in ArchiveRelation]
    acl_sql = """SELECT c.oid, c.relacl::text,
        COALESCE((SELECT string_agg(a.attnum::text || ':' || a.attacl::text, ',' ORDER BY a.attnum)
                  FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attacl IS NOT NULL), '')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname = ANY(%s) ORDER BY c.oid"""
    before_acl = admin.execute(acl_sql, (archive_names,)).fetchall()
    before_events = admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_archive_fence.staged'"
    ).fetchone()
    statements: list[str] = []
    mutex = Lock()
    lock_issued = Event()
    result: Queue[ArchiveFenceStagedObservation | BaseException] = Queue()

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        with mutex:
            statements.append(statement)
        if statement.startswith("LOCK TABLE "):
            lock_issued.set()

    def install() -> None:
        try:
            result.put(_install(engine))
        except BaseException as failure:
            result.put(failure)

    # Hold the last deterministic object, so PostgreSQL must wait after it may
    # have acquired earlier locks. This is a separate real clone connection.
    with psycopg.connect(admin.info.dsn) as blocker:
        blocker.execute(
            sql.SQL("LOCK TABLE public.{} IN ACCESS SHARE MODE").format(
                sql.Identifier(last.value)
            )
        )
        event.listen(engine, "before_cursor_execute", capture)
        worker = Thread(target=install, name="private-archive-lock-canary")
        try:
            worker.start()
            assert lock_issued.wait(timeout=10), (
                "installer never issued the all-table lock"
            )
            waiting = False
            for _ in range(100):
                waiting = admin.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND usename=%s AND wait_event_type='Lock' AND query LIKE 'LOCK TABLE %%')",
                    (engine.url.username,),
                ).fetchone() == (True,)
                if waiting:
                    break
                Event().wait(0.01)
            assert waiting, (
                "installer was not observed waiting on the held archive lock"
            )
            with mutex:
                while_blocked = tuple(statements)
            expected_lock = (
                "LOCK TABLE "
                + ", ".join(
                    'public."' + relation.value + '"'
                    for relation in sorted(ArchiveRelation, key=lambda item: item.value)
                )
                + " IN ACCESS EXCLUSIVE MODE"
            )
            assert [
                statement
                for statement in while_blocked
                if statement.startswith("LOCK TABLE ")
            ] == [expected_lock]
            assert not any(
                statement.lstrip()
                .upper()
                .startswith(
                    (
                        "CREATE ",
                        "ALTER ",
                        "REVOKE ",
                        "GRANT ",
                        "INSERT ",
                        "UPDATE ",
                        "DELETE ",
                        "TRUNCATE ",
                    )
                )
                for statement in while_blocked
            )
            # External visibility alone cannot prove no attempted uncommitted
            # DDL. The listener above captures actual issued statements.
            worker.join(timeout=10)
            assert not worker.is_alive(), "bounded owner lock wait did not terminate"
            failure = result.get_nowait()
            assert isinstance(failure, DBAPIError)
            assert isinstance(failure.orig, psycopg.errors.LockNotAvailable)
            with mutex:
                assert not any(
                    statement.lstrip()
                    .upper()
                    .startswith(
                        (
                            "CREATE ",
                            "ALTER ",
                            "REVOKE ",
                            "GRANT ",
                            "INSERT ",
                            "UPDATE ",
                            "DELETE ",
                            "TRUNCATE ",
                        )
                    )
                    for statement in statements
                )
        finally:
            blocker.rollback()
            worker.join(timeout=10)
            event.remove(engine, "before_cursor_execute", capture)
            assert not worker.is_alive()
    assert admin.execute(acl_sql, (archive_names,)).fetchall() == before_acl
    assert admin.execute(
        "SELECT to_regprocedure('public.dotmac_billing_archive_deny_v1()') IS NULL"
    ).fetchone() == (True,)
    assert admin.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'dotmac_billing_archive_%'"
    ).fetchone() == (0,)
    assert (
        admin.execute(
            "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_archive_fence.staged'"
        ).fetchone()
        == before_events
    )
