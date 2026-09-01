"""Live-PostgreSQL proof of the module schema prerequisite contract.

Until 2026-08-31 this contract had no live test at all. It was covered by unit
tests over hand-built observation objects and by architecture tests that read
source strings — neither of which can notice that the verifier never actually
asks PostgreSQL anything. `mod_inbox` reached two release candidates and
production unprovisioned behind exactly that gap.

What is proved here, against a real migrated database:

- the bootstrap CREATES a missing schema with the right owner;
- `PUBLIC` is DENIED, tested as denial — `SET ROLE` plus a real qualified
  access attempt that must raise `insufficient_privilege` — with a positive
  control through the identical path, because an assertion that can only ever
  fail proves a broken probe just as well as an absent grant;
- all three roles hold effective `USAGE`, transitively;
- a rerun is idempotent and reports `already_satisfied` WITHOUT writing;
- planted drift is caught;
- the restricted migration role cannot do the bootstrap's job, so a deployment
  that reaches for the wrong credential fails rather than half-succeeding.

Every mutating case runs inside a transaction that is rolled back: PostgreSQL
DDL is transactional, so this can drop and rebuild a live module schema without
leaving a mark.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege

from app.commercial_module_prereqs import (
    PUBLIC_PROBE_ROLE,
    commercial_schema_violations,
    module_schema_contract,
)
from scripts.bootstrap_commercial_module_prereqs import (
    Outcome,
    observe_schemas,
    run_bootstrap,
)
from scripts.ci.migrated_test_database import parse_test_database_target

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dsn() -> str:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL lane")
    target = parse_test_database_target(configured)
    return target.url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def rollback_conn(dsn: str):
    """A connection whose every write is discarded."""
    with psycopg.connect(dsn, autocommit=False) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture(scope="module")
def sample_schema(dsn: str) -> str:
    """A module schema that actually has a table in it.

    The positive control below reads a real table through the same code path as
    the denial assertion. A schema with no tables would push the control onto a
    CREATE attempt that `app_user` is also denied, and the control would then
    "fail correctly" for the wrong reason.
    """
    with psycopg.connect(dsn, autocommit=False) as conn:
        for item in module_schema_contract():
            if _a_table_in(conn, item.schema) is not None:
                return item.schema
    pytest.skip(
        "no composed module schema has a table; the access-path control cannot "
        "be exercised, so denial would be unfalsifiable here"
    )


def _a_table_in(conn: psycopg.Connection, schema: str) -> str | None:
    row = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename "
        "LIMIT 1",
        (schema,),
    ).fetchone()
    return str(row[0]) if row else None


def _attempt_access(conn: psycopg.Connection, role: str, schema: str) -> None:
    """Really touch the schema as ``role``. Raises on denial.

    Two things have to be true at once. The access must run inside a SAVEPOINT,
    because a denial aborts the current (sub)transaction and every later
    statement in it — including the ``RESET ROLE`` needed to clean up — then
    fails with ``InFailedSqlTransaction``; the caller would see that instead of
    the ``InsufficientPrivilege`` it is asserting on, and be green on the wrong
    exception. And the savepoint must not discard drift a calling test planted
    outside it.

    The table lookup happens BEFORE ``SET ROLE``, as the privileged connection:
    whether a table exists is a fact about the schema, not about what the probe
    can see, and resolving it through the probe would make an empty result
    ambiguous. ``RESET ROLE`` stays in a ``finally`` outside the savepoint —
    rolling back to the savepoint already restores the GUC, so this is a
    belt-and-braces reset that is safe to run on the healthy outer transaction.
    """
    table = _a_table_in(conn, schema)
    try:
        with conn.transaction():
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            if table is not None:
                conn.execute(
                    sql.SQL("SELECT 1 FROM {}.{} LIMIT 0").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                )
            else:
                # No table to read: creating one needs CREATE on the schema,
                # which PUBLIC must also not hold.
                conn.execute(
                    sql.SQL("CREATE TABLE {}.probe_canary (id int)").format(
                        sql.Identifier(schema)
                    )
                )
    finally:
        conn.execute("RESET ROLE")


def test_the_contract_holds_on_the_migrated_database(dsn: str) -> None:
    """The load-bearing assertion, in the form the deploy owner makes it."""
    with psycopg.connect(dsn, autocommit=False) as conn:
        assert commercial_schema_violations(observe_schemas(conn)) == ()


def test_public_is_denied_and_the_probe_path_actually_works(
    rollback_conn: psycopg.Connection, sample_schema: str
) -> None:
    """Denial, plus the positive control that keeps it honest."""
    with pytest.raises(InsufficientPrivilege):
        _attempt_access(rollback_conn, PUBLIC_PROBE_ROLE, sample_schema)

    # Positive control through the IDENTICAL path. Without it, a typo in the
    # schema name, a missing table or a broken SET ROLE would raise something
    # that looks like proof of denial and is not.
    _attempt_access(rollback_conn, "app_user", sample_schema)


def test_every_usage_role_holds_effective_transitive_usage(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=False) as conn:
        for item in module_schema_contract():
            for role in item.usage_roles:
                granted = conn.execute(
                    "SELECT has_schema_privilege(%s, %s, 'USAGE')",
                    (role, item.schema),
                ).fetchone()
                assert granted and granted[0], (
                    f"{role} lacks effective USAGE on {item.schema}"
                )
            probe = conn.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE') "
                "OR has_schema_privilege(%s, %s, 'CREATE')",
                (PUBLIC_PROBE_ROLE, item.schema, PUBLIC_PROBE_ROLE, item.schema),
            ).fetchone()
            assert probe is not None and not probe[0], (
                f"{PUBLIC_PROBE_ROLE} can reach {item.schema}"
            )


def test_bootstrap_creates_a_dropped_schema_to_full_contract(
    rollback_conn: psycopg.Connection, sample_schema: str
) -> None:
    rollback_conn.execute(
        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(sample_schema))
    )
    assert any(
        "is missing" in violation
        for violation in commercial_schema_violations(observe_schemas(rollback_conn))
    )

    result = run_bootstrap(rollback_conn, dry_run=False, repair=True)

    assert result.outcome is Outcome.REPAIRED
    assert result.exit_code == 0
    assert result.schemas_created == 1
    assert commercial_schema_violations(observe_schemas(rollback_conn)) == ()

    observed = observe_schemas(rollback_conn)[sample_schema]
    assert observed.owner_role == "dotmac_app"
    assert observed.public_privileges == ()
    assert observed.probe_observed is True
    assert observed.probe_privileges == ()
    assert set(observed.usage_roles) == {"app_admin", "app_user", "platform_api"}


def test_a_satisfied_database_reports_already_satisfied_and_writes_nothing(
    rollback_conn: psycopg.Connection,
) -> None:
    """`already_satisfied` must be distinguishable from `repaired`.

    Conflating them is half of the original defect; the other half was
    conflating both with `blocked`.
    """
    result = run_bootstrap(rollback_conn, dry_run=False, repair=True)
    assert result.outcome is Outcome.ALREADY_SATISFIED
    assert result.exit_code == 0
    assert result.schemas_created == 0
    assert result.schemas_regranted == 0
    assert result.roles_created == 0


def test_planted_ownership_drift_is_caught(
    rollback_conn: psycopg.Connection, sample_schema: str
) -> None:
    rollback_conn.execute(
        sql.SQL("ALTER SCHEMA {} OWNER TO app_admin").format(
            sql.Identifier(sample_schema)
        )
    )
    violations = commercial_schema_violations(observe_schemas(rollback_conn))
    assert any("is owned by 'app_admin'" in violation for violation in violations)


def test_planted_public_grant_is_caught_by_the_probe(
    rollback_conn: psycopg.Connection, sample_schema: str
) -> None:
    rollback_conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(
            sql.Identifier(sample_schema)
        )
    )
    observed = observe_schemas(rollback_conn)[sample_schema]
    # Both halves must see it, but the probe is the one that would still see a
    # grant arriving by some route other than a schema ACL row.
    assert "USAGE" in observed.probe_privileges
    assert "USAGE" in observed.public_privileges
    assert any(
        "is reachable by dotmac_public_probe" in violation
        for violation in commercial_schema_violations(observe_schemas(rollback_conn))
    )
    # Effective privilege, not an ACL row: the probe now really does hold USAGE.
    # Note it still cannot read a table — schema USAGE is not table SELECT — so
    # asserting a successful access attempt here would be asserting a falsehood.
    granted = rollback_conn.execute(
        "SELECT has_schema_privilege(%s, %s, 'USAGE')",
        (PUBLIC_PROBE_ROLE, sample_schema),
    ).fetchone()
    assert granted is not None and granted[0]


def test_planted_missing_usage_grant_is_caught(
    rollback_conn: psycopg.Connection, sample_schema: str
) -> None:
    rollback_conn.execute(
        sql.SQL("REVOKE USAGE ON SCHEMA {} FROM app_user").format(
            sql.Identifier(sample_schema)
        )
    )
    violations = commercial_schema_violations(observe_schemas(rollback_conn))
    assert any("does not grant USAGE to app_user" in v for v in violations)


def test_the_repair_is_idempotent(
    rollback_conn: psycopg.Connection, sample_schema: str
) -> None:
    rollback_conn.execute(
        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(sample_schema))
    )
    first = run_bootstrap(rollback_conn, dry_run=False, repair=True)
    second = run_bootstrap(rollback_conn, dry_run=False, repair=True)

    assert first.outcome is Outcome.REPAIRED
    assert second.outcome is Outcome.ALREADY_SATISFIED
    assert second.schemas_created == 0
    assert commercial_schema_violations(observe_schemas(rollback_conn)) == ()


def test_the_restricted_migration_role_cannot_do_the_bootstraps_job(
    rollback_conn: psycopg.Connection,
) -> None:
    """Why a migration cannot own schema creation, asserted rather than stated.

    This is also the property the 2026-08-31 production attempt violated: it
    connected with the APPLICATION credential and expected schema-creation
    powers. Whatever password it had used, this is what it would have hit.
    """
    granted = rollback_conn.execute(
        "SELECT has_database_privilege('dotmac_app', current_database(), 'CREATE')"
    ).fetchone()
    assert granted is not None and not granted[0], (
        "dotmac_app must never hold database-level CREATE (ADR-0011)"
    )

    rollback_conn.execute("SET ROLE dotmac_app")
    try:
        with pytest.raises(InsufficientPrivilege):
            rollback_conn.execute("CREATE SCHEMA mod_should_not_be_creatable")
    finally:
        rollback_conn.rollback()
