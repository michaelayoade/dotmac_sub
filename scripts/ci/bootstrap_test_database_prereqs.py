#!/usr/bin/env python3
"""Bootstrap database-local prerequisites for disposable PostgreSQL tests.

The integration gate first prepares the explicit TEST_DATABASE_URL, then many
migration rehearsal tests create additional databases with PostgreSQL's default
``CREATE DATABASE`` behaviour. Those databases inherit database-local schema ACLs
from ``template1``, not from TEST_DATABASE_URL. This disposable CI adapter
explicitly prepares immutable revision 557's historical role membership before
fresh replay; operational deployment verification refuses that retired link.
It applies the full contract to the explicit test database and only the
inherited public-schema outbox contract to ``template1`` so module-schema
creation remains under Alembic test coverage.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    SCHEMA_BOOTSTRAP_ROLE,
)
from app.outbox_dispatcher_roles import (
    HISTORICAL_557_RELAY_OWNERSHIP_CONTRACT,
    relay_dispatcher_violations,
)
from scripts.bootstrap_commercial_module_prereqs import (
    bootstrap as bootstrap_commercial_module_prereqs,
)
from scripts.bootstrap_commercial_module_prereqs import observe_roles
from scripts.bootstrap_outbox_dispatcher_roles import (
    bootstrap as bootstrap_outbox_dispatcher_roles,
)
from scripts.bootstrap_outbox_dispatcher_roles import (
    ensure_current_schema_privileges,
    observe_historical_membership,
)
from scripts.bootstrap_outbox_dispatcher_roles import (
    observe as observe_dispatchers,
)
from scripts.ci.migrated_test_database import (
    DatabaseContractError,
    DatabaseTarget,
    parse_test_database_target,
)
from scripts.testing.host_test_policy import (
    HostTestPolicyError,
    require_full_suite_host,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPOSABLE_CLUSTER_NAME = "dotmac-sub-disposable-tests"


@dataclass(frozen=True, slots=True)
class DisposableClusterObservation:
    name: str
    setting_context: str


class DisposableClusterRefusal(RuntimeError):
    """CI historical preparation lacks its explicit disposable-cluster proof."""


def _require_disposable_cluster(
    conn: psycopg.Connection,
    url: URL,
    *,
    environ: Mapping[str, str],
) -> DisposableClusterObservation:
    """Require the checked-in CI cluster marker before privileged test writes.

    PostgreSQL's postmaster-context cluster_name cannot be set by this session.
    The marker is purpose evidence, not authentication or operator approval.
    """

    if environ.get("APP_ENV") not in {"development", "test"}:
        raise DisposableClusterRefusal("APP_ENV must identify a test environment")
    try:
        require_full_suite_host(repo_root=REPO_ROOT, environ=environ)
        target = parse_test_database_target(environ.get("TEST_DATABASE_URL"))
    except (HostTestPolicyError, DatabaseContractError) as exc:
        raise DisposableClusterRefusal("test host or target is not approved") from exc
    if (url.host, url.port or 5432) != (
        target.url.host,
        target.url.port or 5432,
    ):
        raise DisposableClusterRefusal("database endpoint differs from test target")
    row = conn.execute(
        "SELECT setting, context FROM pg_settings WHERE name = 'cluster_name'"
    ).fetchone()
    if row is None:
        raise DisposableClusterRefusal("disposable cluster marker is unavailable")
    observed = DisposableClusterObservation(str(row[0]), str(row[1]))
    if observed != DisposableClusterObservation(DISPOSABLE_CLUSTER_NAME, "postmaster"):
        raise DisposableClusterRefusal("disposable cluster marker is absent")
    return observed


def _psycopg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _prepare_historical_557_replay(conn: psycopg.Connection, url: URL) -> None:
    """CI-only preparation for immutable revision 557 on a marked test cluster."""

    _require_disposable_cluster(conn, url, environ=os.environ)
    contract = HISTORICAL_557_RELAY_OWNERSHIP_CONTRACT
    roles = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
        ([contract.migration_role, contract.definer_role],),
    ).fetchall()
    if {str(row[0]) for row in roles} != {
        contract.migration_role,
        contract.definer_role,
    }:
        raise DisposableClusterRefusal("historical migration roles are missing")
    membership_row = conn.execute(
        "SELECT pg_has_role(%s, %s, 'MEMBER')",
        (contract.migration_role, contract.definer_role),
    ).fetchone()
    if membership_row is None:
        raise DisposableClusterRefusal("historical membership evidence is unavailable")
    if not membership_row[0]:
        conn.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(contract.definer_role),
                sql.Identifier(contract.migration_role),
            )
        )


def _bootstrap_outbox_url(url: URL, *, label: str, template_only: bool = False) -> int:
    with psycopg.connect(_psycopg_url(url), autocommit=False) as conn:
        _require_disposable_cluster(conn, url, environ=os.environ)
        if template_only:
            # CREATE DATABASE clones template1's database-local public ACL.
            ensure_current_schema_privileges(conn, dry_run=False)
        elif observe_historical_membership(conn):
            # The first disposable bootstrap already prepared the cluster-wide
            # link. Clone databases need their local public-schema privilege;
            # never route the legacy link through operational repair.
            if relay_dispatcher_violations(observe_dispatchers(conn)):
                print("disposable dispatcher posture drift", file=sys.stderr)
                return 1
            ensure_current_schema_privileges(conn, dry_run=False)
        else:
            outbox_result = bootstrap_outbox_dispatcher_roles(
                conn, dry_run=False, repair=True
            )
            if outbox_result != 0:
                print(
                    f"failed outbox dispatcher prerequisite bootstrap for {label}",
                    file=sys.stderr,
                )
                return outbox_result
            _prepare_historical_557_replay(conn, url)
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
    else:
        membership_row = conn.execute(
            "SELECT pg_has_role(%s, 'app_admin', 'MEMBER')", (SCHEMA_BOOTSTRAP_ROLE,)
        ).fetchone()
        if membership_row is not None and membership_row[0]:
            return 0
        print(
            "schema-bootstrap test role lacks owner membership; provisioning refused",
            file=sys.stderr,
        )
        return 2
    return 0


def bootstrap_disposable_database(url: URL, *, label: str) -> int:
    with psycopg.connect(_psycopg_url(url), autocommit=False) as conn:
        _require_disposable_cluster(conn, url, environ=os.environ)
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
        # On this marked disposable cluster alone, actual app_user/app_admin
        # login tests use the synthetic server password from TEST_DATABASE_URL.
        # Refuse drift before changing either cluster-wide credential.
        if url.password is not None:
            observed = observe_roles(conn)
            for role in ("app_user", "app_admin"):
                if (
                    observed.get(role)
                    != COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT[role].authority_posture
                ):
                    print(
                        "disposable database login posture drift; credential setup refused",
                        file=sys.stderr,
                    )
                    return 2
            conn.execute(
                sql.SQL("ALTER ROLE app_user PASSWORD {}").format(
                    sql.Literal(url.password)
                )
            )
            conn.execute(
                sql.SQL("ALTER ROLE app_admin PASSWORD {}").format(
                    sql.Literal(url.password)
                )
            )
    return _bootstrap_outbox_url(url, label=label)


def _bootstrap_template_public_schema(target: DatabaseTarget) -> int:
    return _bootstrap_outbox_url(
        target.url.set(database="template1"), label="template1", template_only=True
    )


def main() -> int:
    try:
        target = parse_test_database_target(os.getenv("TEST_DATABASE_URL"))
    except DatabaseContractError as exc:
        print(f"REFUSED [{exc.code.value}] {exc}", file=sys.stderr)
        return 2

    try:
        test_target = bootstrap_disposable_database(
            target.url, label=target.database_name
        )
        if test_target != 0:
            return test_target

        template = _bootstrap_template_public_schema(target)
        if template != 0:
            return template
        return 0
    except DisposableClusterRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
