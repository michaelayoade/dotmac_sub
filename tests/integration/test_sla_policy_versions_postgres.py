"""Migrated-PostgreSQL canaries for `sla_policy_versions` (OUTAGE_SLA_SPINE §4).

Per the approved standard, a database-backed test claiming deployed behaviour
must run against PostgreSQL with a schema built by the **real Alembic chain** —
`Base.metadata.create_all()` is not deployed-schema evidence. These constraints
are exactly the kind that metadata cannot express and a model-built schema
silently omits:

- `ex_sla_policy_versions_no_overlap` — a GiST exclusion constraint. It exists
  only in migration 467 and is the reason "the policy in force at instant T"
  has a single answer. An application-level check cannot replace it: two
  concurrent writers would each read before writing and both pass.
- `ck_sla_policy_versions_scope_matches_source` — binds the precedence claim
  to the scope column.
- `ck_sla_policy_versions_contractual_target` — a contractual source may not
  omit its availability target, because the design forbids inventing one.

Both migration proofs the standard asks for are covered:

- **fresh acceptance** — baseline → head builds the table with its constraints;
- **incremental acceptance** — the real predecessor (466) → head proves an
  existing production database gains them. This matters because revision 001
  builds from the current `Base.metadata`, so a fresh upgrade would create the
  table even if migration 467 did nothing; the incremental path is what
  actually exercises the new DDL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import errors as pg_errors
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from alembic import command

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "466_team_inbox_channel_ai_routes"
CANDIDATE = "467_sla_policy_versions"

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def engine():
    """Satisfy the package PostgreSQL guard without building current schema.

    The package fixture calls `create_all()`, which would bypass the migration
    path under test — the whole point of this module.
    """
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("migrated-schema test requires TEST_DATABASE_URL")
    url = make_url(configured)
    if not url.drivername.startswith("postgresql"):
        pytest.skip("migrated-schema test requires PostgreSQL")

    class _Dialect:
        name = "postgresql"

    class _Stub:
        dialect = _Dialect()

    return _Stub()


@pytest.fixture
def migrated_database() -> Iterator[URL]:
    """A disposable database built by the real Alembic chain, to head."""
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("migrated-schema test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        pytest.skip("migrated-schema test requires PostgreSQL")

    name = f"dotmac_sla_policy_{uuid.uuid4().hex}"
    maintenance = base.set(database="postgres")
    with psycopg.connect(_render(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = base.set(database=name)
    try:
        yield target
    finally:
        with psycopg.connect(_render(maintenance), autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


def _alembic(url: URL, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    command.upgrade(config, revision)


def _constraints(url: URL) -> dict[str, str]:
    with psycopg.connect(_render(url)) as conn:
        rows = conn.execute(
            """
            SELECT conname, contype
            FROM pg_constraint
            WHERE conrelid = 'sla_policy_versions'::regclass
            """
        ).fetchall()
    return {name: kind for name, kind in rows}


def _insert(conn, **overrides) -> None:
    payload = {
        "id": uuid.uuid4(),
        "policy_key": "policy:acceptance",
        "version": 1,
        "source": "internal_measurement",
        "subscription_id": None,
        "subscriber_id": None,
        "offer_id": None,
        "effective_from": NOW,
        "effective_to": None,
        "availability_target_percent": 99.5,
        "calendar_timezone": "Africa/Lagos",
        "maintenance_excludable": True,
        "created_at": NOW,
    }
    payload.update(overrides)
    conn.execute(
        """
        INSERT INTO sla_policy_versions
          (id, policy_key, version, source, subscription_id, subscriber_id,
           offer_id, effective_from, effective_to, availability_target_percent,
           calendar_timezone, maintenance_excludable, created_at)
        VALUES
          (%(id)s, %(policy_key)s, %(version)s, %(source)s, %(subscription_id)s,
           %(subscriber_id)s, %(offer_id)s, %(effective_from)s, %(effective_to)s,
           %(availability_target_percent)s, %(calendar_timezone)s,
           %(maintenance_excludable)s, %(created_at)s)
        """,
        payload,
    )


# --- fresh acceptance -------------------------------------------------------


def test_head_builds_the_table_with_its_migration_only_constraints(
    engine, migrated_database
):
    _alembic(migrated_database, "head")

    found = _constraints(migrated_database)
    assert "ex_sla_policy_versions_no_overlap" in found
    assert found["ex_sla_policy_versions_no_overlap"] == "x", "must be EXCLUDE"
    for check in (
        "ck_sla_policy_versions_scope_matches_source",
        "ck_sla_policy_versions_contractual_target",
        "ck_sla_policy_versions_range",
        "ck_sla_policy_versions_target_bounds",
    ):
        assert check in found, f"{check} missing from the migrated schema"


# --- incremental acceptance -------------------------------------------------


def test_existing_production_database_gains_the_constraints(engine, migrated_database):
    """The proof that matters: predecessor → candidate, not baseline → head.

    Revision 001 builds from current `Base.metadata`, so a fresh upgrade would
    create this table even if 467 were a no-op. Stopping at 466 and stepping
    forward is what actually exercises the new DDL.
    """
    _alembic(migrated_database, PREDECESSOR)
    with psycopg.connect(_render(migrated_database)) as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.sla_policy_versions')"
        ).fetchone()[0]
    # Revision 001's metadata bootstrap may or may not have created it; either
    # way the constraint the candidate owns must be absent beforehand.
    before = _constraints(migrated_database) if exists else {}
    assert "ex_sla_policy_versions_no_overlap" not in before

    _alembic(migrated_database, CANDIDATE)

    after = _constraints(migrated_database)
    assert "ex_sla_policy_versions_no_overlap" in after


# --- the constraints actually bite ------------------------------------------


def test_overlapping_versions_of_one_policy_are_rejected(engine, migrated_database):
    """Two versions covering the same instant would make "the policy in force
    at T" ambiguous. Only the database can enforce this against concurrency."""
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        _insert(
            conn, version=1, effective_from=NOW, effective_to=NOW + timedelta(days=30)
        )
        with pytest.raises(pg_errors.ExclusionViolation) as caught:
            _insert(
                conn,
                version=2,
                effective_from=NOW + timedelta(days=10),
                effective_to=NOW + timedelta(days=40),
            )
    assert caught.value.diag.constraint_name == "ex_sla_policy_versions_no_overlap"


def test_abutting_versions_of_one_policy_are_allowed(engine, migrated_database):
    """Half-open ranges must let one version end exactly where the next
    begins — otherwise a lawful policy change could not be recorded."""
    _alembic(migrated_database, "head")
    boundary = NOW + timedelta(days=30)

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        _insert(conn, version=1, effective_from=NOW, effective_to=boundary)
        _insert(conn, version=2, effective_from=boundary, effective_to=None)

        count = conn.execute(
            "SELECT count(*) FROM sla_policy_versions WHERE policy_key = %s",
            ("policy:acceptance",),
        ).fetchone()[0]
    assert count == 2


def test_a_precedence_claim_requires_its_scope(engine, migrated_database):
    _alembic(migrated_database, "head")

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        # subscription_contract with no subscription_id
        with pytest.raises(pg_errors.CheckViolation) as caught:
            _insert(conn, source="subscription_contract", subscription_id=None)
    assert (
        caught.value.diag.constraint_name
        == "ck_sla_policy_versions_scope_matches_source"
    )


def test_a_contractual_policy_may_not_omit_its_target(engine, migrated_database):
    """The design forbids inventing a target, so the schema forbids a
    contractual row without one — while still allowing the internal
    measurement policy to stay silent about what was promised."""
    _alembic(migrated_database, "head")
    subscription_id = uuid.uuid4()

    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        with pytest.raises(pg_errors.CheckViolation) as caught:
            _insert(
                conn,
                source="account_contract",
                subscription_id=None,
                subscriber_id=subscription_id,
                availability_target_percent=None,
                policy_key="acct:no-target",
            )
    # CHECK constraints are evaluated before the FK trigger fires, so this is
    # the target rule biting and not the unrelated subscriber FK.
    assert (
        caught.value.diag.constraint_name == "ck_sla_policy_versions_contractual_target"
    )

    # The other direction: internal_measurement legitimately has no target,
    # and the constraint must not block it.
    with psycopg.connect(_render(migrated_database), autocommit=True) as conn:
        _insert(
            conn,
            source="internal_measurement",
            subscription_id=None,
            availability_target_percent=None,
            policy_key="internal-ok",
        )
        stored = conn.execute(
            "SELECT availability_target_percent FROM sla_policy_versions "
            "WHERE policy_key = %s",
            ("internal-ok",),
        ).fetchone()[0]
    assert stored is None
