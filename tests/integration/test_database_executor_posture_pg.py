"""Actual-login refusals on a disposable PostgreSQL database.

These tests change only database-local grants. Cluster role flags and passwords
are never changed here; concurrent tests may use those same roles.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL

from alembic import command
from app.commercial_module_prereqs import SCHEMA_BOOTSTRAP_ROLE
from scripts import verify_database_connection_pair as pair
from scripts.bootstrap_commercial_module_prereqs import (
    EXIT_BLOCKED,
    Outcome,
    run_bootstrap,
    verify,
)


def _psycopg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def isolated_database(template_base_url: URL) -> Iterator[URL]:
    """Create only this test's database; do not alter shared cluster roles."""

    name = f"dotmac_test_posture_{uuid4().hex}"
    maintenance = template_base_url.set(database="postgres")
    with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as elevated:
        elevated.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield template_base_url.set(database=name)
    finally:
        with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as elevated:
            elevated.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            elevated.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def _version_table_absent(conn: psycopg.Connection) -> bool:
    return conn.execute(
        "SELECT to_regclass('public.alembic_version') IS NULL"
    ).fetchone()[0]


def _migration_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    return config


def test_elevated_actual_login_refused_before_alembic_version(
    isolated_database: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    with psycopg.connect(_psycopg_url(isolated_database)) as elevated:
        actual = elevated.execute("SELECT session_user, current_user").fetchone()
        assert actual is not None and actual[0] == actual[1] != "app_admin"
        assert _version_table_absent(elevated)
        monkeypatch.setenv(
            "MIGRATION_DATABASE_URL",
            isolated_database.render_as_string(hide_password=False),
        )
        with pytest.raises(RuntimeError, match="actual app_admin login"):
            command.upgrade(_migration_config(), "heads")
        assert _version_table_absent(elevated)


def test_app_admin_with_database_create_is_refused_before_version_write(
    isolated_database: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_name = isolated_database.database
    assert database_name is not None
    with psycopg.connect(_psycopg_url(isolated_database)) as elevated:
        elevated.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO app_admin").format(
                sql.Identifier(database_name)
            )
        )
        elevated.commit()
        assert _version_table_absent(elevated)

    admin_url = isolated_database.set(username="app_admin")
    with psycopg.connect(_psycopg_url(admin_url)) as migration_login:
        assert migration_login.execute(
            "SELECT session_user, current_user, "
            "has_database_privilege(session_user, current_database(), 'CREATE')"
        ).fetchone() == ("app_admin", "app_admin", True)
        assert verify(migration_login) == EXIT_BLOCKED
        assert _version_table_absent(migration_login)
        monkeypatch.setenv(
            "MIGRATION_DATABASE_URL", admin_url.render_as_string(hide_password=False)
        )
        with pytest.raises(RuntimeError, match="no database CREATE"):
            command.upgrade(_migration_config(), "heads")
        assert _version_table_absent(migration_login)


def test_schema_repair_requires_actual_bootstrap_login_and_named_create(
    isolated_database: URL,
) -> None:
    database_name = isolated_database.database
    assert database_name is not None
    with psycopg.connect(_psycopg_url(isolated_database)) as elevated:
        if elevated.execute(
            "SELECT to_regrole(%s) IS NOT NULL", (SCHEMA_BOOTSTRAP_ROLE,)
        ).fetchone() != (True,):
            pytest.fail("PostgreSQL test cluster needs the schema-bootstrap role")
        elevated.execute(
            sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                sql.Identifier(database_name), sql.Identifier(SCHEMA_BOOTSTRAP_ROLE)
            )
        )
        elevated.commit()

    with psycopg.connect(
        _psycopg_url(isolated_database.set(username="app_admin"))
    ) as migration_login:
        assert migration_login.execute(
            "SELECT session_user, current_user"
        ).fetchone() == ("app_admin", "app_admin")
        wrong_login = run_bootstrap(
            migration_login, dry_run=False, repair=True, allow_role_creation=False
        )
        assert wrong_login.outcome is Outcome.BLOCKED
        assert wrong_login.exit_code == EXIT_BLOCKED

    # This is an authenticated connection as the bootstrap role, not SET ROLE
    # from a superuser. The disposable database has no named CREATE grant.
    try:
        bootstrap_login = psycopg.connect(
            _psycopg_url(isolated_database.set(username=SCHEMA_BOOTSTRAP_ROLE))
        )
    except psycopg.OperationalError:
        pytest.fail("PostgreSQL test fixture needs an actual schema-bootstrap login")
    with bootstrap_login:
        assert bootstrap_login.execute(
            "SELECT session_user, current_user"
        ).fetchone() == (SCHEMA_BOOTSTRAP_ROLE, SCHEMA_BOOTSTRAP_ROLE)
        no_named_create = run_bootstrap(
            bootstrap_login, dry_run=False, repair=True, allow_role_creation=False
        )
        assert no_named_create.outcome is Outcome.BLOCKED
        assert no_named_create.exit_code == EXIT_BLOCKED
        assert "named database CREATE" in (no_named_create.blocked_reason or "")

    with psycopg.connect(_psycopg_url(isolated_database)) as elevated:
        elevated.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(database_name), sql.Identifier(SCHEMA_BOOTSTRAP_ROLE)
            )
        )
        elevated.commit()
    with psycopg.connect(
        _psycopg_url(isolated_database.set(username=SCHEMA_BOOTSTRAP_ROLE))
    ) as bootstrap_login:
        repaired = run_bootstrap(
            bootstrap_login, dry_run=False, repair=True, allow_role_creation=False
        )
        assert repaired.exit_code == 0
        assert repaired.schemas_created > 0
        owners = bootstrap_login.execute(
            "SELECT DISTINCT pg_get_userbyid(nspowner) FROM pg_namespace "
            "WHERE nspname LIKE 'mod\\_%' ESCAPE '\\'"
        ).fetchall()
        assert owners == [("app_admin",)]


def test_actual_runtime_and_migration_logins_share_disposable_backend(
    isolated_database: URL,
) -> None:
    runtime_url = isolated_database.set(username="app_user")
    migration_url = isolated_database.set(username="app_admin")
    runtime = pair.observe_connection(_psycopg_url(runtime_url))
    migration = pair.observe_connection(_psycopg_url(migration_url))

    assert runtime is not None and migration is not None
    assert (
        runtime.principal.session_user == runtime.principal.current_user == "app_user"
    )
    assert (
        migration.principal.session_user
        == migration.principal.current_user
        == "app_admin"
    )
    assert runtime.backend.database == isolated_database.database
    assert pair.pair_refusal(runtime, migration) is None


def test_actual_logins_on_distinct_disposable_databases_are_refused(
    isolated_database: URL,
    template_base_url: URL,
) -> None:
    other_name = f"dotmac_test_pair_{uuid4().hex}"
    maintenance = template_base_url.set(database="postgres")
    with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as elevated:
        elevated.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(other_name))
        )
    try:
        runtime_url = isolated_database.set(username="app_user")
        migration_url = isolated_database.set(username="app_admin", database=other_name)
        runtime = pair.observe_connection(_psycopg_url(runtime_url))
        migration = pair.observe_connection(_psycopg_url(migration_url))

        assert runtime is not None and migration is not None
        assert runtime.backend.database == isolated_database.database
        assert migration.backend.database == other_name
        assert runtime.backend.address == migration.backend.address
        assert runtime.backend.port == migration.backend.port
        assert (
            runtime.backend.postmaster_started_at
            == migration.backend.postmaster_started_at
        )
        assert pair.pair_refusal(runtime, migration) == "backend_mismatch"
    finally:
        with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as elevated:
            elevated.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (other_name,),
            )
            elevated.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(other_name))
            )
