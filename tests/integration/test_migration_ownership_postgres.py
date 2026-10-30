"""The migration runtime grants against real PostgreSQL catalogs.

The migrator is app_admin acting as itself (``SET ROLE`` here, because the
fixture connects as a superuser and the catalog only needs ``current_user``);
the runtime role is dotmac_app. This is the Starter/ERP model: app_admin owns
every new object, dotmac_app is granted DML, dotmac_app holds no CREATE on
``public`` and cannot run DDL on what the migrator created.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import URL

from app.migration_ownership import (
    MigrationOwnershipError,
    assert_migrator_identity,
    grant_runtime_privileges,
    snapshot_owned_objects,
)
from scripts.bootstrap_commercial_module_prereqs import bootstrap as bootstrap_modules
from scripts.bootstrap_outbox_dispatcher_roles import bootstrap as bootstrap_outbox
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database
_psycopg_url = kernel_rehearsal._psycopg_url


@pytest.fixture
def migrator_connection(isolated_database: URL):
    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as admin:
        assert bootstrap_modules(admin, dry_run=False, repair=True) == 0
        assert bootstrap_outbox(admin, dry_run=False, repair=True) == 0
        # Production shape: the migrator may create in public, the runtime role
        # may only use it. The old test hand-granted CREATE to dotmac_app.
        admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        admin.execute("REVOKE CREATE ON SCHEMA public FROM dotmac_app")
        admin.execute("GRANT USAGE ON SCHEMA public TO dotmac_app")
        admin.execute("GRANT USAGE, CREATE ON SCHEMA public TO app_admin")
        admin.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO app_admin").format(
                sql.Identifier(isolated_database.database)
            )
        )
        admin.commit()
        assert admin.execute(
            "SELECT has_schema_privilege('dotmac_app', 'public', 'CREATE'), "
            "has_schema_privilege('app_admin', 'public', 'CREATE')"
        ).fetchone() == (False, True)
    with psycopg.connect(_psycopg_url(isolated_database), autocommit=False) as conn:
        conn.execute("SET ROLE app_admin")
        yield conn
        conn.rollback()


def grant(conn, *, before, allowlist=frozenset(), **kw) -> int:
    return grant_runtime_privileges(
        conn,
        runtime_role="dotmac_app",
        before=before,
        definer_allowlist=allowlist,
        **kw,
    )


def as_runtime(conn) -> None:
    conn.execute("RESET ROLE")
    conn.execute("SET ROLE dotmac_app")


def as_migrator(conn) -> None:
    conn.execute("RESET ROLE")
    conn.execute("SET ROLE app_admin")


def denied(conn, statement: str) -> None:
    """The statement fails with a privilege error (savepoint keeps the txn)."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute(statement)


def _owners(connection, names: list[str]) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT relname, pg_get_userbyid(relowner) FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s)",
            (names,),
        )
    }


def test_identity_check_accepts_the_contract_migrator(migrator_connection) -> None:
    assert_migrator_identity(migrator_connection, owner="dotmac_app")


def test_identity_check_refuses_the_owner_itself(migrator_connection) -> None:
    migrator_connection.execute("RESET ROLE")
    migrator_connection.execute("SET ROLE dotmac_app")
    with pytest.raises(MigrationOwnershipError, match="module_database_roles.v1"):
        assert_migrator_identity(migrator_connection, owner="dotmac_app")


def test_new_objects_stay_with_the_migrator_and_the_runtime_is_granted(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE TABLE public.zz_own_t (id serial PRIMARY KEY, v text UNIQUE)")
    conn.execute("CREATE INDEX zz_own_ix ON public.zz_own_t (v)")
    conn.execute(
        "CREATE VIEW public.zz_own_v WITH (security_invoker = true) "
        "AS SELECT id FROM public.zz_own_t"
    )
    conn.execute("CREATE SEQUENCE public.zz_own_seq")
    conn.execute("CREATE TYPE public.zz_own_mood AS ENUM ('a', 'b')")
    conn.execute("CREATE DOMAIN public.zz_own_dom AS integer CHECK (VALUE > 0)")
    conn.execute(
        "CREATE FUNCTION public.zz_own_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
    )

    granted = grant(conn, before=before)

    # table, view, standalone sequence, serial's sequence
    assert granted == 4
    owners = _owners(
        conn,
        [
            "zz_own_t",
            "zz_own_t_pkey",
            "zz_own_t_v_key",
            "zz_own_ix",
            "zz_own_v",
            "zz_own_seq",
            "zz_own_t_id_seq",
        ],
    )
    assert set(owners.values()) == {"app_admin"}, owners
    for relation, privs in {
        "public.zz_own_t": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        "public.zz_own_v": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    }.items():
        for priv in privs:
            assert conn.execute(
                "SELECT has_table_privilege('dotmac_app', %s::regclass, %s)",
                (relation, priv),
            ).fetchone() == (True,), (relation, priv)
    for sequence in ("public.zz_own_seq", "public.zz_own_t_id_seq"):
        for priv in ("USAGE", "SELECT", "UPDATE"):
            assert conn.execute(
                "SELECT has_sequence_privilege('dotmac_app', %s::regclass, %s)",
                (sequence, priv),
            ).fetchone() == (True,), (sequence, priv)
    # Nothing was transferred.
    assert (
        conn.execute(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'zz_own_fn'"
        ).fetchone()[0]
        == "app_admin"
    )


def test_runtime_role_can_dml_the_new_table_and_use_its_sequence(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE TABLE public.zz_dml_t (id serial PRIMARY KEY, v text)")
    conn.execute("CREATE SEQUENCE public.zz_dml_seq")
    grant(conn, before=before)

    as_runtime(conn)
    # The serial default calls nextval(): needs USAGE on the column's sequence.
    row = conn.execute(
        "INSERT INTO public.zz_dml_t (v) VALUES ('a') RETURNING id"
    ).fetchone()
    assert row == (1,)
    assert conn.execute("SELECT v FROM public.zz_dml_t").fetchall() == [("a",)]
    conn.execute("UPDATE public.zz_dml_t SET v = 'b' WHERE id = 1")
    assert conn.execute("SELECT nextval('public.zz_dml_seq')").fetchone() == (1,)
    assert conn.execute("SELECT last_value FROM public.zz_dml_t_id_seq").fetchone()
    conn.execute("DELETE FROM public.zz_dml_t WHERE id = 1")


def test_runtime_role_cannot_run_ddl_on_a_migrator_owned_table(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE TABLE public.zz_ddl_t (id serial PRIMARY KEY, v text)")
    grant(conn, before=before)

    as_runtime(conn)
    denied(conn, "ALTER TABLE public.zz_ddl_t ADD COLUMN w text")
    denied(conn, "ALTER TABLE public.zz_ddl_t OWNER TO dotmac_app")
    denied(conn, "DROP TABLE public.zz_ddl_t")
    denied(conn, "CREATE INDEX zz_ddl_ix ON public.zz_ddl_t (v)")
    denied(conn, "TRUNCATE public.zz_ddl_t")
    denied(conn, "ALTER SEQUENCE public.zz_ddl_t_id_seq RESTART")
    denied(conn, "DROP SEQUENCE public.zz_ddl_t_id_seq CASCADE")


def test_runtime_role_cannot_create_objects_in_public(migrator_connection) -> None:
    conn = migrator_connection
    as_runtime(conn)
    denied(conn, "CREATE TABLE public.zz_rt_created (id int)")
    denied(conn, "CREATE SEQUENCE public.zz_rt_seq")


def test_runtime_is_still_bound_by_row_level_security_on_a_migrator_owned_table(
    migrator_connection,
) -> None:
    """The migrator owns the table, so it bypasses RLS; the runtime does not."""
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE TABLE public.zz_rls_t (id int)")
    conn.execute("ALTER TABLE public.zz_rls_t ENABLE ROW LEVEL SECURITY")
    conn.execute("CREATE POLICY zz_none ON public.zz_rls_t USING (false)")
    conn.execute("INSERT INTO public.zz_rls_t VALUES (1)")
    grant(conn, before=before)
    assert conn.execute("SELECT count(*) FROM public.zz_rls_t").fetchone() == (1,)
    as_runtime(conn)
    assert conn.execute("SELECT count(*) FROM public.zz_rls_t").fetchone() == (0,)


def test_security_invoker_view_is_readable_by_the_runtime_but_not_alterable(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE TABLE public.zz_vt (id int)")
    conn.execute(
        "CREATE VIEW public.zz_vv WITH (security_invoker = on) "
        "AS SELECT id FROM public.zz_vt"
    )
    grant(conn, before=before)
    as_runtime(conn)
    assert conn.execute("SELECT count(*) FROM public.zz_vv").fetchone() == (0,)
    denied(conn, "ALTER VIEW public.zz_vv OWNER TO dotmac_app")


def test_existing_runtime_owned_objects_are_left_alone(migrator_connection) -> None:
    conn = migrator_connection
    conn.execute("RESET ROLE")
    conn.execute("CREATE TABLE public.zz_old_t (id int)")
    conn.execute("ALTER TABLE public.zz_old_t OWNER TO dotmac_app")
    as_migrator(conn)
    before = snapshot_owned_objects(conn)
    # Mixed ownership: the migrator reaches the old table through membership.
    conn.execute("ALTER TABLE public.zz_old_t ADD COLUMN w text")
    conn.execute("CREATE TABLE public.zz_new_t (id int)")
    assert grant(conn, before=before) == 1
    assert _owners(conn, ["zz_old_t", "zz_new_t"]) == {
        "zz_old_t": "dotmac_app",
        "zz_new_t": "app_admin",
    }


def test_the_version_table_bootstrap_grant_is_read_only(migrator_connection) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE TABLE public.zz_version (version_num varchar(255))")
    assert grant(conn, before=before, read_only=True) == 1
    as_runtime(conn)
    assert conn.execute("SELECT count(*) FROM public.zz_version").fetchone() == (0,)
    denied(conn, "INSERT INTO public.zz_version VALUES ('x')")


def test_an_unlisted_security_definer_function_aborts_the_run(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute(
        "CREATE FUNCTION public.zz_own_definer() RETURNS integer LANGUAGE sql "
        "SECURITY DEFINER SET search_path = '' AS 'SELECT 1'"
    )
    with pytest.raises(MigrationOwnershipError, match="not in the definer allowlist"):
        grant(conn, before=before)


def test_a_listed_security_definer_function_stays_with_the_migrator(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute(
        "CREATE FUNCTION public.zz_own_definer(integer) RETURNS integer LANGUAGE sql "
        "SECURITY DEFINER SET search_path = '' AS 'SELECT 1'"
    )
    grant(conn, before=before, allowlist=frozenset({"public.zz_own_definer(integer)"}))
    assert (
        conn.execute(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'zz_own_definer'"
        ).fetchone()[0]
        == "app_admin"
    )


def test_a_materialized_view_is_refused(migrator_connection) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE MATERIALIZED VIEW public.zz_own_mv AS SELECT 1 AS x")
    with pytest.raises(MigrationOwnershipError, match="materialized view"):
        grant(conn, before=before)


@pytest.mark.parametrize(
    "view_ddl",
    [
        "CREATE VIEW public.zz_own_bad_v AS SELECT id FROM public.zz_own_vt",
        "CREATE VIEW public.zz_own_bad_v WITH (security_invoker = false) "
        "AS SELECT id FROM public.zz_own_vt",
        "CREATE VIEW public.zz_own_bad_v WITH (security_barrier = true) "
        "AS SELECT id FROM public.zz_own_vt",
    ],
)
def test_a_view_without_security_invoker_is_refused(
    migrator_connection, view_ddl: str
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE TABLE public.zz_own_vt (id int)")
    conn.execute(view_ddl)
    with pytest.raises(MigrationOwnershipError, match="security_invoker"):
        grant(conn, before=before)


def test_a_new_schema_is_refused_rather_than_skipped(migrator_connection) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.execute("CREATE SCHEMA zz_own_schema")
    with pytest.raises(MigrationOwnershipError, match="pg_namespace"):
        grant(conn, before=before)
