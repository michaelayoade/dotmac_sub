"""Actual-login diagnostics on a clone of the real migration chain.

The hosted PostgreSQL 16 shard job owns this disposable cluster. A generated
test-only password authenticates app_user; no verifier is retrieved or printed.
Pristine posture, absent memberships and an originally NULL password are proved
before mutation. A maintenance-database advisory lock coordinates this fixture's
role mutations; it cannot coordinate arbitrary processes ignoring that lock.
The hosted job's per-shard service and serial suite provide cluster isolation.
These canaries do not claim module write denial or final handoff admission.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus
from sqlalchemy.engine import URL

from app.commercial_module_prereqs import (
    BillingRuntimeLoginQuery,
    BillingRuntimeLoginRefusal,
    BillingRuntimeLoginRefused,
    require_billing_runtime_login,
)


@dataclass(frozen=True)
class RuntimeConnections:
    admin: psycopg.Connection
    runtime: psycopg.Connection


def _require_pristine_app_user(admin: psycopg.Connection) -> None:
    # Return only a boolean. Never retrieve, retain or output a password verifier.
    row = admin.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_authid AS r
             WHERE r.rolname = 'app_user' AND r.rolpassword IS NULL
               AND r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls
                   OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication)
               AND NOT EXISTS (
                   SELECT 1 FROM pg_catalog.pg_auth_members AS m
                    WHERE m.member = r.oid OR m.roleid = r.oid
               )
        )
        """
    ).fetchone()
    if row != (True,):
        raise pytest.UsageError(
            "runtime login canaries require pristine app_user on the disposable CI cluster"
        )


@pytest.fixture
def runtime_connections(
    template_base_url: URL,
    cloned_database: Callable[[str], URL],
) -> Iterator[RuntimeConnections]:
    # This is the existing integration-shards service contract, not a new
    # credential or an opt-in allowing mutation of a shared development host.
    if not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("CI") == "true"
        and os.environ.get("GITHUB_JOB") == "integration-shards"
        and template_base_url.host == "localhost"
        and template_base_url.port == 5432
        and template_base_url.database == "dotmac_sub_test"
        and template_base_url.username == "postgres"
    ):
        raise pytest.UsageError(
            "runtime login canaries require the isolated hosted integration-shards PostgreSQL service"
        )
    with psycopg.connect(
        template_base_url.set(
            drivername="postgresql", database="postgres"
        ).render_as_string(hide_password=False),
        autocommit=True,
    ) as role_admin:
        assert role_admin.info.server_version >= 160000
        # Advisory locks are database-scoped: every fixture uses postgres,
        # never its private clone, and holds this connection until cleanup.
        role_admin.execute("SELECT pg_advisory_lock(72701, 16001)")
        try:
            _require_pristine_app_user(role_admin)
            database = cloned_database("heads")
            # Migration preparation must not have changed the proven baseline.
            _require_pristine_app_user(role_admin)
            ephemeral_password = uuid4().hex
            password_owned = False
            try:
                role_admin.execute(
                    sql.SQL("ALTER ROLE app_user PASSWORD {}").format(
                        sql.Literal(ephemeral_password)
                    )
                )
                password_owned = True
                with (
                    psycopg.connect(
                        database.set(drivername="postgresql").render_as_string(
                            hide_password=False
                        ),
                        autocommit=True,
                    ) as admin,
                    psycopg.connect(
                        database.set(
                            drivername="postgresql",
                            username="app_user",
                            password=ephemeral_password,
                        ).render_as_string(hide_password=False),
                        autocommit=False,
                    ) as runtime,
                ):
                    yield RuntimeConnections(admin=admin, runtime=runtime)
            finally:
                if password_owned:
                    # Restore exactly the proven original NULL state, including
                    # failed connection setup or test-body failures. Do not hide
                    # restoration errors or repair unowned posture/memberships.
                    role_admin.execute("ALTER ROLE app_user PASSWORD NULL")
                    _require_pristine_app_user(role_admin)
        finally:
            row = role_admin.execute(
                "SELECT pg_advisory_unlock(72701, 16001)"
            ).fetchone()
            assert row == (True,)


def _refused(conn: psycopg.Connection, code: BillingRuntimeLoginRefusal) -> None:
    with pytest.raises(BillingRuntimeLoginRefused) as refusal:
        require_billing_runtime_login(conn, query=BillingRuntimeLoginQuery())
    assert refusal.value.code is code


def test_actual_login_keeps_backend_transaction_and_observes_catalog_changes(
    runtime_connections: RuntimeConnections,
) -> None:
    admin, runtime = runtime_connections.admin, runtime_connections.runtime
    _refused(runtime, BillingRuntimeLoginRefusal.INACTIVE_TRANSACTION)
    runtime.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
    before = runtime.execute(
        "SELECT pg_backend_pid(), pg_current_xact_id()::text, session_user, current_user"
    ).fetchone()
    assert before is not None
    observed = require_billing_runtime_login(runtime)
    assert before[2:] == ("app_user", "app_user")
    assert observed.backend_pid == before[0]
    assert observed.transaction_id == before[1]
    assert observed.session_role_oid == observed.current_role_oid
    assert runtime.info.transaction_status == TransactionStatus.INTRANS

    # A later committed ownership change is visible in this same backend and
    # same READ COMMITTED transaction. Diagnostics cannot be cached as proof.
    admin.execute("CREATE TABLE public.runtime_login_owner_canary (id integer)")
    admin.execute("ALTER TABLE public.runtime_login_owner_canary OWNER TO app_user")
    _refused(runtime, BillingRuntimeLoginRefusal.OWNERSHIP)
    admin.execute("DROP TABLE public.runtime_login_owner_canary")
    assert require_billing_runtime_login(runtime) == observed

    # A change made by the supplied backend itself is also immediately seen.
    admin.execute("CREATE SCHEMA runtime_login_canary")
    admin.execute("GRANT USAGE, CREATE ON SCHEMA runtime_login_canary TO app_user")
    runtime.execute("CREATE TABLE runtime_login_canary.owned_here (id integer)")
    _refused(runtime, BillingRuntimeLoginRefusal.OWNERSHIP)
    runtime.execute("DROP TABLE runtime_login_canary.owned_here")
    assert require_billing_runtime_login(runtime) == observed
    runtime.rollback()
    admin.execute("DROP SCHEMA runtime_login_canary")

    # Temporary ownership is still ownership; no pg_* namespace exemption.
    runtime.execute("CREATE TEMP TABLE runtime_login_temporary_canary (id integer)")
    try:
        _refused(runtime, BillingRuntimeLoginRefusal.OWNERSHIP)
    finally:
        runtime.rollback()

    # The elevated fixture creates only this synthetic function in the private
    # migrated clone. Commit makes its ownership visible to the actual login.
    # No privileged helper is executed, and teardown removes the exact object.
    admin.execute(
        "CREATE FUNCTION pg_catalog.runtime_login_catalog_owner_canary() "
        "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
    )
    try:
        admin.execute(
            "ALTER FUNCTION pg_catalog.runtime_login_catalog_owner_canary() "
            "OWNER TO app_user"
        )
        runtime.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
        _refused(runtime, BillingRuntimeLoginRefusal.OWNERSHIP)
    finally:
        runtime.rollback()
        admin.execute("DROP FUNCTION pg_catalog.runtime_login_catalog_owner_canary()")


def test_admin_role_impersonation_is_not_authentication(
    runtime_connections: RuntimeConnections,
) -> None:
    admin = runtime_connections.admin
    admin.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
    try:
        admin.execute("SET LOCAL ROLE app_user")
        _refused(admin, BillingRuntimeLoginRefusal.IDENTITY)
        admin.execute("RESET ROLE")
        admin.execute("SET SESSION AUTHORIZATION app_user")
        assert admin.execute("SELECT session_user, current_user").fetchone() == (
            "app_user",
            "app_user",
        )
        _refused(admin, BillingRuntimeLoginRefusal.IDENTITY)
    finally:
        # ROLLBACK remains legal in INERROR; reset must not prevent cleanup.
        admin.execute("ROLLBACK")
        admin.execute("RESET SESSION AUTHORIZATION")


@pytest.mark.parametrize(
    "options",
    (
        "INHERIT TRUE, SET FALSE, ADMIN FALSE",
        "INHERIT FALSE, SET TRUE, ADMIN FALSE",
        "INHERIT FALSE, SET FALSE, ADMIN TRUE",
        "INHERIT FALSE, SET FALSE, ADMIN FALSE",
    ),
    ids=("inherit", "set", "dormant-admin", "all-false-dormant"),
)
def test_unknown_inherit_set_and_dormant_admin_paths_refuse(
    runtime_connections: RuntimeConnections,
    options: str,
) -> None:
    admin, runtime = runtime_connections.admin, runtime_connections.runtime
    role = sql.Identifier(f"runtime_login_path_{uuid4().hex}")
    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
    runtime.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
    try:
        admin.execute(sql.SQL("GRANT {} TO app_user WITH " + options).format(role))
        _refused(runtime, BillingRuntimeLoginRefusal.ROLE_PATH)
        admin.execute(sql.SQL("REVOKE {} FROM app_user").format(role))
        require_billing_runtime_login(runtime)
    finally:
        admin.execute(sql.SQL("REVOKE {} FROM app_user").format(role))
        admin.execute(sql.SQL("DROP ROLE {}").format(role))


def test_dormant_membership_with_transitive_role_path_refuses(
    runtime_connections: RuntimeConnections,
) -> None:
    admin, runtime = runtime_connections.admin, runtime_connections.runtime
    role = sql.Identifier(f"runtime_login_path_{uuid4().hex}")
    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
    runtime.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
    try:
        second_role = sql.Identifier(f"runtime_login_target_{uuid4().hex}")
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(second_role))
        try:
            admin.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(
                    second_role, role
                )
            )
            admin.execute(
                sql.SQL(
                    "GRANT {} TO app_user WITH INHERIT FALSE, SET FALSE, ADMIN FALSE"
                ).format(role)
            )
            _refused(runtime, BillingRuntimeLoginRefusal.ROLE_PATH)
        finally:
            admin.execute(sql.SQL("REVOKE {} FROM app_user").format(role))
            admin.execute(sql.SQL("REVOKE {} FROM {}").format(second_role, role))
            admin.execute(sql.SQL("DROP ROLE {}").format(second_role))
    finally:
        admin.execute(sql.SQL("REVOKE {} FROM app_user").format(role))
        admin.execute(sql.SQL("DROP ROLE {}").format(role))


def test_privileged_flags_and_snapshot_isolation_refuse(
    runtime_connections: RuntimeConnections,
) -> None:
    admin, runtime = runtime_connections.admin, runtime_connections.runtime
    runtime.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    _refused(runtime, BillingRuntimeLoginRefusal.ISOLATION)
    runtime.rollback()
    runtime.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
    for flag in ("SUPERUSER", "BYPASSRLS", "CREATEROLE", "CREATEDB", "REPLICATION"):
        try:
            admin.execute(sql.SQL("ALTER ROLE app_user " + flag))
            _refused(runtime, BillingRuntimeLoginRefusal.PRIVILEGED_ROLE)
        finally:
            admin.execute(sql.SQL("ALTER ROLE app_user NO" + flag))
        require_billing_runtime_login(runtime)


def test_aborted_transaction_cannot_become_login_evidence(
    runtime_connections: RuntimeConnections,
) -> None:
    runtime = runtime_connections.runtime
    runtime.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
    # An already aborted transaction is refused without repairing it.
    with pytest.raises(psycopg.errors.DivisionByZero):
        runtime.execute("SELECT 1 / 0")
    _refused(runtime, BillingRuntimeLoginRefusal.INACTIVE_TRANSACTION)
    assert runtime.info.transaction_status == TransactionStatus.INERROR
