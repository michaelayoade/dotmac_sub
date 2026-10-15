"""Real-chain retained-column canaries on the existing isolated actual-login fixture.

No metadata schema, fallback role, delivery transport or local database is used.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.subscriber import Subscriber
from app.services.billing.legacy_retained_columns_contracts import (
    RetainedColumnsRequest,
    RetainedColumnsStagedObservation,
    RetainedRelation,
)
from app.services.billing.legacy_retained_columns_fence import (
    RetainedColumnsRefused,
    _install_legacy_retained_columns_fence,
)
from app.services.owner_commands import CommandContext, execute_owner_command
from tests.integration.test_billing_legacy_archive_fence_postgres import (
    _DEFINITION,
    FenceConnections,
    fence_connections,  # noqa: F401 -- isolated actual owner fixture
)
from tests.integration.test_billing_legacy_archive_fence_postgres import (
    _stage as _archive_stage,
)
from tests.integration.test_billing_runtime_login_postgres import (
    runtime_connections,  # noqa: F401 -- transitive migrated clone fixture
)

pytestmark = pytest.mark.integration


_SUB_COLUMNS = "id,first_name,last_name,email,email_verified,gender,status,user_type,is_active,marketing_opt_in,reseller_id,billing_enabled,captive_redirect_enabled,billing_mode,created_at,updated_at,deposit"
_INSERT_SUBSCRIBER = (
    "INSERT INTO subscribers (" + _SUB_COLUMNS + ") VALUES "  # noqa: S608 -- fixed closed test columns
    "(%s,'Retained','Canary','retained@example.invalid',false,'unknown','new','customer',true,false,"
    "(SELECT id FROM resellers WHERE is_house),true,false,'postpaid',now(),now(),%s)"
)


@pytest.fixture
def retained_connections(
    fence_connections: FenceConnections,  # noqa: F811 -- imported fixture
) -> Iterator[FenceConnections]:
    for name in ("billing_accounts", "subscribers"):
        fence_connections.admin.execute(
            sql.SQL("ALTER TABLE public.{} OWNER TO {}").format(
                sql.Identifier(name),
                sql.Identifier(fence_connections.owner.url.username),
            )
        )
        fence_connections.admin.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON public.{} TO app_user"
            ).format(sql.Identifier(name))
        )
    yield fence_connections


def _stage(db: Session) -> RetainedColumnsStagedObservation:
    db.execute(text("SET LOCAL lock_timeout = '2s'"))
    db.execute(text("SET LOCAL statement_timeout = '30s'"))
    return _install_legacy_retained_columns_fence(db, request=RetainedColumnsRequest())


def _install(engine: Engine) -> RetainedColumnsStagedObservation:
    with Session(engine) as db:
        return execute_owner_command(
            db,
            definition=_DEFINITION,
            context=CommandContext.system(
                actor="retained-canary",
                scope="private-clone",
                reason="retained component acceptance",
                idempotency_key="retained-component-v1",
            ),
            operation=lambda: _stage(db),
        )


def _subscriber(connection: psycopg.Connection, deposit: str | None) -> UUID:
    identity = uuid4()
    connection.execute(
        _INSERT_SUBSCRIBER,
        (identity, deposit),
    )
    return identity


def _denied(
    connection: psycopg.Connection, statement: str, parameters: tuple[object, ...] = ()
) -> None:
    connection.rollback()
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege,
        match="legacy retained monetary value is immutable",
    ):
        connection.execute(statement, parameters)
    connection.rollback()


def test_deposit_history_runtime_writes_and_exact_replay(
    retained_connections: FenceConnections,
) -> None:
    admin, runtime = retained_connections.admin, retained_connections.runtime
    historic = [_subscriber(admin, amount) for amount in ("12.50", "-7.25", "0", None)]
    first = _install(retained_connections.owner)
    replay = _install(retained_connections.owner)
    assert not first.replay_observed and replay.replay_observed
    assert first.catalog_digest == replay.catalog_digest
    assert admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_retained_columns.staged'"
    ).fetchone() == (1,)
    assert runtime.execute("SELECT session_user,current_user").fetchone() == (
        "app_user",
        "app_user",
    )
    runtime.rollback()
    for identity in historic:
        runtime.execute(
            "UPDATE subscribers SET deposit=deposit,min_balance=9.25,prepaid_low_balance_at=now(),mrr_total=4.50,first_name=first_name,is_active=false WHERE id=%s",
            (identity,),
        )
        runtime.commit()
        _denied(runtime, "UPDATE subscribers SET deposit=100 WHERE id=%s", (identity,))
        _denied(
            runtime, "UPDATE subscribers SET id=%s WHERE id=%s", (uuid4(), identity)
        )
    for identity in historic[:3]:
        _denied(runtime, "DELETE FROM subscribers WHERE id=%s", (identity,))
    runtime.execute("DELETE FROM subscribers WHERE id=%s", (historic[3],))
    runtime.commit()
    pristine = _subscriber(runtime, None)
    runtime.commit()
    for value in ("0", "1", "-1"):
        _denied(
            runtime,
            _INSERT_SUBSCRIBER,
            (uuid4(), value),
        )
    _denied(runtime, "UPDATE subscribers SET deposit=0 WHERE id=%s", (pristine,))
    # Bulk atomicity: the pristine row cannot disappear alongside protected history.
    _denied(
        runtime, "DELETE FROM subscribers WHERE id IN (%s,%s)", (pristine, historic[0])
    )
    assert runtime.execute(
        "SELECT count(*) FROM subscribers WHERE id IN (%s,%s)", (pristine, historic[0])
    ).fetchone() == (2,)
    runtime.rollback()
    _denied(
        runtime,
        _INSERT_SUBSCRIBER + " ON CONFLICT(id) DO UPDATE SET deposit=0",
        (pristine, None),
    )
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege,
        match="legacy retained monetary value is immutable",
    ):
        with runtime.cursor().copy(
            "COPY subscribers(" + _SUB_COLUMNS + ") FROM STDIN"
        ) as copy:
            copy.write_row(
                (
                    uuid4(),
                    "Copy",
                    "Canary",
                    "copy@example.invalid",
                    False,
                    "unknown",
                    "new",
                    "customer",
                    True,
                    False,
                    admin.execute("SELECT id FROM resellers WHERE is_house").fetchone()[
                        0
                    ],
                    True,
                    False,
                    "postpaid",
                    datetime.now(UTC),
                    datetime.now(UTC),
                    0,
                )
            )
    runtime.rollback()
    # ORM uses the same actual runtime login, never impersonation via SET ROLE.
    runtime_engine = create_engine(
        "postgresql+psycopg://", creator=lambda: runtime, poolclass=StaticPool
    )
    try:
        with Session(runtime_engine) as db:
            row = db.get(Subscriber, pristine)
            assert row is not None
            row.deposit = Decimal("0")
            with pytest.raises(
                Exception, match="legacy retained monetary value is immutable"
            ):
                db.flush()
            db.rollback()
    finally:
        runtime_engine.dispose(close=False)


def test_final_tuple_column_grants_replica_and_truncate(
    retained_connections: FenceConnections,
) -> None:
    admin, runtime = retained_connections.admin, retained_connections.runtime
    identity = _subscriber(admin, None)
    _install(retained_connections.owner)
    admin.execute(
        "GRANT UPDATE(deposit), INSERT(deposit) ON subscribers TO app_user WITH GRANT OPTION"
    )
    admin.execute(
        "CREATE FUNCTION public.retained_late_money() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN NEW.deposit := 5; RETURN NEW; END;$$"
    )
    admin.execute(
        "CREATE TRIGGER zzzz_retained_late_money BEFORE UPDATE ON subscribers FOR EACH ROW EXECUTE FUNCTION public.retained_late_money()"
    )
    _denied(
        runtime, "UPDATE subscribers SET first_name=first_name WHERE id=%s", (identity,)
    )
    admin.execute("DROP TRIGGER zzzz_retained_late_money ON subscribers")
    # Parameter ACLs are cluster-wide. The shared fixture holds its maintenance
    # lock; prove absence, then restore exactly even if a canary fails.
    assert admin.execute(
        "SELECT has_parameter_privilege('app_user','session_replication_role','SET')"
    ).fetchone() == (False,)
    admin.execute("GRANT SET ON PARAMETER session_replication_role TO app_user")
    try:
        runtime.execute("SET session_replication_role='replica'")
        runtime.commit()
        _denied(runtime, "UPDATE subscribers SET deposit=0 WHERE id=%s", (identity,))
    finally:
        runtime.rollback()
        runtime.execute("SET session_replication_role='origin'")
        runtime.commit()
        admin.execute(
            "REVOKE SET ON PARAMETER session_replication_role FROM app_user RESTRICT"
        )
        assert admin.execute(
            "SELECT has_parameter_privilege('app_user','session_replication_role','SET')"
        ).fetchone() == (False,)
    admin.execute("GRANT TRUNCATE ON ALL TABLES IN SCHEMA public TO app_user")
    _denied(runtime, "TRUNCATE subscribers CASCADE")
    with pytest.raises(RetainedColumnsRefused):
        _install(retained_connections.owner)


def test_composed_owner_rollback_and_catalog_drift(
    retained_connections: FenceConnections,
) -> None:
    class InjectedFailure(Exception):
        pass

    with Session(retained_connections.owner) as db:

        def operation() -> None:
            # Final owner must prelock the complete union before invoking participants.
            from app.services.billing.legacy_archive_fence_contracts import (
                ArchiveRelation,
            )

            names = sorted(
                [r.value for r in ArchiveRelation] + ["subscribers", "billing_accounts"]
            )
            db.execute(text("SET LOCAL lock_timeout='2s'"))
            db.execute(text("SET LOCAL statement_timeout='30s'"))
            db.execute(
                text(
                    "LOCK TABLE "
                    + ", ".join('public."' + n + '"' for n in names)
                    + " IN ACCESS EXCLUSIVE MODE"
                )
            )
            _archive_stage(db)
            _stage(db)
            raise InjectedFailure

        with pytest.raises(InjectedFailure):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary",
                    scope="clone",
                    reason="composed rollback",
                    idempotency_key="archive-retained-composed-rollback-v1",
                ),
                operation=operation,
            )
    admin = retained_connections.admin
    assert admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type IN ('billing.legacy_archive_fence.staged','billing.legacy_retained_columns.staged')"
    ).fetchone() == (0,)
    assert admin.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'dotmac_retained_%' OR tgname LIKE 'dotmac_billing_archive_%'"
    ).fetchone() == (0,)
    _install(retained_connections.owner)
    admin.execute(
        "ALTER TABLE subscribers DISABLE TRIGGER dotmac_retained_final_row_v1"
    )
    with pytest.raises(RetainedColumnsRefused):
        _install(retained_connections.owner)


def test_account_balance_create_preserve_config_and_delete(
    retained_connections: FenceConnections,
) -> None:
    admin, runtime = retained_connections.admin, retained_connections.runtime

    def reseller() -> UUID:
        identity = uuid4()
        admin.execute(
            "INSERT INTO resellers(id,name,is_active,is_house,created_at,updated_at) VALUES (%s,'Retained account',true,false,now(),now())",
            (identity,),
        )
        return identity

    def account(balance: str) -> UUID:
        identity = uuid4()
        admin.execute(
            "INSERT INTO billing_accounts(id,reseller_id,name,balance) VALUES (%s,%s,'Historic',%s)",
            (identity, reseller(), balance),
        )
        return identity

    positive, negative, zero = (account(value) for value in ("10.25", "-1.25", "0"))
    _install(retained_connections.owner)
    for identity in (positive, negative, zero):
        runtime.execute(
            "UPDATE billing_accounts SET name='Retained configuration',balance=balance WHERE id=%s",
            (identity,),
        )
        runtime.commit()
        _denied(
            runtime, "UPDATE billing_accounts SET balance=1 WHERE id=%s", (identity,)
        )
        _denied(
            runtime,
            "UPDATE billing_accounts SET id=%s WHERE id=%s",
            (uuid4(), identity),
        )
    for identity in (positive, negative):
        _denied(runtime, "DELETE FROM billing_accounts WHERE id=%s", (identity,))
    runtime.execute("DELETE FROM billing_accounts WHERE id=%s", (zero,))
    runtime.commit()
    for amount in ("1", "-1"):
        _denied(
            runtime,
            "INSERT INTO billing_accounts(id,reseller_id,name,balance) VALUES (%s,%s,'Denied',%s)",
            (uuid4(), reseller(), amount),
        )
    # NOT NULL is the migration's earlier guard; NULL never becomes an absent balance.
    with pytest.raises(psycopg.errors.NotNullViolation):
        runtime.execute(
            "INSERT INTO billing_accounts(id,reseller_id,name,balance) VALUES (%s,%s,'Null refused',NULL)",
            (uuid4(), reseller()),
        )
    runtime.rollback()
    identity = uuid4()
    runtime.execute(
        "INSERT INTO billing_accounts(id,reseller_id,name,balance) VALUES (%s,%s,'Pristine',0)",
        (identity, reseller()),
    )
    runtime.commit()
    runtime.execute("DELETE FROM billing_accounts WHERE id=%s", (identity,))
    runtime.commit()


@pytest.mark.parametrize(
    "change",
    ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", "SET TRANSACTION READ ONLY"),
)
def test_retained_posture_refuses(
    retained_connections: FenceConnections, change: str
) -> None:
    from app.services.billing.legacy_archive_fence import ArchiveFenceRefused

    with Session(retained_connections.owner) as db:

        def invalid() -> object:
            db.execute(text(change))
            return _stage(db)

        with pytest.raises(ArchiveFenceRefused):
            execute_owner_command(
                db,
                definition=_DEFINITION,
                context=CommandContext.system(
                    actor="canary",
                    scope="clone",
                    reason="retained posture refusal",
                    idempotency_key="retained-posture-refusal-v1",
                ),
                operation=invalid,
            )


def test_both_locks_precede_any_ddl(
    retained_connections: FenceConnections,
) -> None:
    from queue import Queue
    from threading import Event, Lock, Thread

    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    admin, engine = retained_connections.admin, retained_connections.owner
    last = sorted(RetainedRelation, key=lambda relation: relation.value)[-1]
    retained_names = [relation.value for relation in RetainedRelation]
    acl_sql = """SELECT c.oid, c.relacl::text,
        COALESCE((SELECT string_agg(a.attnum::text || ':' || a.attacl::text, ',' ORDER BY a.attnum)
                  FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attacl IS NOT NULL), '')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname = ANY(%s) ORDER BY c.oid"""
    before_acl = admin.execute(acl_sql, (retained_names,)).fetchall()
    before_events = admin.execute(
        "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_retained_columns.staged'"
    ).fetchone()
    statements: list[str] = []
    mutex = Lock()
    lock_issued = Event()
    result: Queue[RetainedColumnsStagedObservation | BaseException] = Queue()

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
        worker = Thread(target=install, name="private-retained-lock-canary")
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
                "installer was not observed waiting on the held retained lock"
            )
            with mutex:
                while_blocked = tuple(statements)
            expected_lock = (
                "LOCK TABLE "
                + ", ".join(
                    'public."' + relation.value + '"'
                    for relation in sorted(
                        RetainedRelation, key=lambda item: item.value
                    )
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
            assert failure.orig.sqlstate == "55P03"
            assert "lock timeout" in str(failure.orig)
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
    assert admin.execute(acl_sql, (retained_names,)).fetchall() == before_acl
    assert admin.execute(
        "SELECT to_regprocedure('public.dotmac_retained_billing_accounts_v1()') IS NULL AND to_regprocedure('public.dotmac_retained_subscribers_v1()') IS NULL"
    ).fetchone() == (True,)
    assert admin.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'dotmac_retained_%'"
    ).fetchone() == (0,)
    assert (
        admin.execute(
            "SELECT count(*) FROM event_store WHERE event_type='billing.legacy_retained_columns.staged'"
        ).fetchone()
        == before_events
    )


@pytest.mark.parametrize("drift", ("precision", "nullability", "owner"))
def test_retained_exact_shape_refuses_before_install(
    retained_connections: FenceConnections, drift: str
) -> None:
    admin = retained_connections.admin
    if drift == "precision":
        admin.execute("ALTER TABLE subscribers ALTER COLUMN deposit TYPE numeric(13,2)")
    elif drift == "nullability":
        admin.execute("ALTER TABLE subscribers ALTER COLUMN deposit SET NOT NULL")
    else:
        admin.execute("ALTER TABLE subscribers OWNER TO postgres")
    with pytest.raises(RetainedColumnsRefused):
        _install(retained_connections.owner)
    assert admin.execute(
        "SELECT count(*) FROM pg_proc WHERE proname IN ('dotmac_retained_billing_accounts_v1','dotmac_retained_subscribers_v1')"
    ).fetchone() == (0,)
