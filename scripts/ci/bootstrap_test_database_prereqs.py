#!/usr/bin/env python3
"""Bootstrap database-local prerequisites for disposable PostgreSQL tests.

The integration gate first prepares the explicit TEST_DATABASE_URL, then many
migration rehearsal tests create additional databases with PostgreSQL's default
``CREATE DATABASE`` behaviour. Those databases inherit database-local schema ACLs
from ``template1``, not from TEST_DATABASE_URL. Production deploys run the same
bootstrap before migrations; this CI adapter applies the full contract to the
explicit test database and only the inherited public-schema outbox contract to
``template1`` so module-schema creation remains under Alembic test coverage.
"""

from __future__ import annotations

import os
import sys

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

from app.commercial_module_prereqs import SCHEMA_BOOTSTRAP_ROLE
from scripts.bootstrap_commercial_module_prereqs import (
    bootstrap as bootstrap_commercial_module_prereqs,
)
from scripts.bootstrap_outbox_dispatcher_roles import (
    bootstrap as bootstrap_outbox_dispatcher_roles,
)
from scripts.ci.migrated_test_database import (
    DatabaseContractError,
    parse_test_database_target,
)


def _psycopg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _bootstrap_outbox_url(url: URL, *, label: str) -> int:
    with psycopg.connect(_psycopg_url(url), autocommit=False) as conn:
        outbox_result = bootstrap_outbox_dispatcher_roles(
            conn, dry_run=False, repair=True
        )
        if outbox_result != 0:
            print(
                f"failed outbox dispatcher prerequisite bootstrap for {label}",
                file=sys.stderr,
            )
            return outbox_result
    print(f"bootstrapped outbox prerequisites for {label}")
    return 0


def _bootstrap_test_schema_login(conn: psycopg.Connection, url: URL) -> int:
    """Provision only a missing synthetic login; refuse existing role drift."""
    identity = conn.execute(
        "SELECT rolcanlogin, rolinherit, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
        "FROM pg_roles WHERE rolname = %s",
        (SCHEMA_BOOTSTRAP_ROLE,),
    ).fetchone()
    if identity is None:
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
            ).format(sql.Identifier(SCHEMA_BOOTSTRAP_ROLE))
        )
        if url.password is not None:
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(SCHEMA_BOOTSTRAP_ROLE), sql.Literal(url.password)
                )
            )
        conn.execute(
            sql.SQL("GRANT app_admin TO {}").format(
                sql.Identifier(SCHEMA_BOOTSTRAP_ROLE)
            )
        )
    elif identity != (True, False, False, False, False, False):
        print(
            "schema-bootstrap test role has unexpected authority; provisioning refused",
            file=sys.stderr,
        )
        return 2
    elif not conn.execute(
        "SELECT pg_has_role(%s, 'app_admin', 'MEMBER')", (SCHEMA_BOOTSTRAP_ROLE,)
    ).fetchone()[0]:
        print(
            "schema-bootstrap test role lacks owner membership; provisioning refused",
            file=sys.stderr,
        )
        return 2
    return 0


def bootstrap_disposable_database(url: URL, *, label: str) -> int:
    with psycopg.connect(_psycopg_url(url), autocommit=False) as conn:
        # Historical revision 001 creates these extensions. PostGIS extension
        # installation belongs to this disposable superuser bootstrap so the
        # actual migration connection can remain app_admin throughout.
        for extension in ("postgis", "postgis_topology", "pg_trgm", "btree_gist"):
            conn.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                    sql.Identifier(extension)
                )
            )
        commercial_result = bootstrap_commercial_module_prereqs(
            conn, dry_run=False, repair=True
        )
        if commercial_result != 0:
            print(
                f"failed commercial module prerequisite bootstrap for {label}",
                file=sys.stderr,
            )
            return commercial_result
        if _bootstrap_test_schema_login(conn, url):
            return 2
        # Disposable CI role login uses the disposable server's test password.
        if url.password is not None:
            conn.execute(
                sql.SQL("ALTER ROLE app_admin PASSWORD {}").format(
                    sql.Literal(url.password)
                )
            )
    return _bootstrap_outbox_url(url, label=label)


def main() -> int:
    try:
        target = parse_test_database_target(os.getenv("TEST_DATABASE_URL"))
    except DatabaseContractError as exc:
        print(f"REFUSED [{exc.code.value}] {exc}", file=sys.stderr)
        return 2

    test_target = bootstrap_disposable_database(target.url, label=target.database_name)
    if test_target != 0:
        return test_target

    template = _bootstrap_outbox_url(
        target.url.set(database="template1"), label="template1"
    )
    if template != 0:
        return template
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
