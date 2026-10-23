"""The ownership hand-back against real PostgreSQL catalogs.

The migrator is app_admin acting as itself (``SET ROLE`` here, because the
fixture connects as a superuser and the catalog only needs ``current_user``);
the owner is dotmac_app.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from app.migration_ownership import (
    MigrationOwnershipError,
    assert_migrator_identity,
    hand_back_new_objects,
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
        # The runtime owner owns the database (and so the public schema).
        admin.execute("GRANT CREATE ON SCHEMA public TO dotmac_app")
        admin.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO app_admin").format(
                sql.Identifier(isolated_database.database)
            )
        )
        admin.commit()
    engine = create_engine(
        isolated_database.set(drivername="postgresql+psycopg"), future=True
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("SET ROLE app_admin")
        yield connection
        connection.rollback()
    engine.dispose()


def _owners(connection, names: list[str]) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in connection.exec_driver_sql(
            "SELECT relname, pg_get_userbyid(relowner) FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s)",
            (names,),
        )
    }


def test_identity_check_accepts_the_contract_migrator(migrator_connection) -> None:
    assert_migrator_identity(migrator_connection, owner="dotmac_app")


def test_identity_check_refuses_the_owner_itself(migrator_connection) -> None:
    migrator_connection.exec_driver_sql("RESET ROLE")
    migrator_connection.exec_driver_sql("SET ROLE dotmac_app")
    with pytest.raises(MigrationOwnershipError, match="module_database_roles.v1"):
        assert_migrator_identity(migrator_connection, owner="dotmac_app")


def test_new_objects_are_handed_back_in_the_same_transaction(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.exec_driver_sql(
        "CREATE TABLE public.zz_own_t (id serial PRIMARY KEY, v text UNIQUE)"
    )
    conn.exec_driver_sql("CREATE INDEX zz_own_ix ON public.zz_own_t (v)")
    conn.exec_driver_sql(
        "CREATE VIEW public.zz_own_v AS SELECT id FROM public.zz_own_t"
    )
    conn.exec_driver_sql("CREATE MATERIALIZED VIEW public.zz_own_mv AS SELECT 1 AS x")
    conn.exec_driver_sql("CREATE SEQUENCE public.zz_own_seq")
    conn.exec_driver_sql("CREATE TYPE public.zz_own_mood AS ENUM ('a', 'b')")
    conn.exec_driver_sql("CREATE DOMAIN public.zz_own_dom AS integer CHECK (VALUE > 0)")
    conn.exec_driver_sql(
        "CREATE FUNCTION public.zz_own_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
    )

    handed = hand_back_new_objects(
        conn, owner="dotmac_app", before=before, definer_allowlist=frozenset()
    )

    assert handed >= 6
    owners = _owners(
        conn,
        [
            "zz_own_t",
            "zz_own_t_pkey",
            "zz_own_t_v_key",
            "zz_own_ix",
            "zz_own_v",
            "zz_own_mv",
            "zz_own_seq",
            "zz_own_t_id_seq",
        ],
    )
    assert set(owners.values()) == {"dotmac_app"}, owners
    assert (
        conn.exec_driver_sql(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'zz_own_fn'"
        ).scalar()
        == "dotmac_app"
    )
    assert (
        conn.exec_driver_sql(
            "SELECT pg_get_userbyid(typowner) FROM pg_type WHERE typname = 'zz_own_mood'"
        ).scalar()
        == "dotmac_app"
    )


def test_an_unlisted_security_definer_function_aborts_the_run(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.exec_driver_sql(
        "CREATE FUNCTION public.zz_own_definer() RETURNS integer LANGUAGE sql "
        "SECURITY DEFINER SET search_path = '' AS 'SELECT 1'"
    )
    with pytest.raises(MigrationOwnershipError, match="not in the definer allowlist"):
        hand_back_new_objects(
            conn, owner="dotmac_app", before=before, definer_allowlist=frozenset()
        )


def test_a_listed_security_definer_function_stays_with_the_migrator(
    migrator_connection,
) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.exec_driver_sql(
        "CREATE FUNCTION public.zz_own_definer(integer) RETURNS integer LANGUAGE sql "
        "SECURITY DEFINER SET search_path = '' AS 'SELECT 1'"
    )
    hand_back_new_objects(
        conn,
        owner="dotmac_app",
        before=before,
        definer_allowlist=frozenset({"public.zz_own_definer(integer)"}),
    )
    assert (
        conn.exec_driver_sql(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'zz_own_definer'"
        ).scalar()
        == "app_admin"
    )


def test_a_new_schema_is_refused_rather_than_skipped(migrator_connection) -> None:
    conn = migrator_connection
    before = snapshot_owned_objects(conn)
    conn.exec_driver_sql("CREATE SCHEMA zz_own_schema")
    with pytest.raises(MigrationOwnershipError, match="pg_namespace"):
        hand_back_new_objects(
            conn, owner="dotmac_app", before=before, definer_allowlist=frozenset()
        )
