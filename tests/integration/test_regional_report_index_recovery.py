"""Real PG lock-timeout and 661-to-662 proof; no hand-created schema."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL

from alembic import command
from scripts.ci import template_database
from scripts.migration import regional_report_billing_indexes as contract

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "661_support_ticket_comment_idempotency"
REPAIR = "662_validate_regional_report_billing_indexes"


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def _database_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def fresh_migration_database(template_base_url: URL, monkeypatch) -> Iterator[URL]:
    """Create an empty disposable database so this module replays the chain."""
    from app import config as app_config

    name = "dotmac_regional_index_migration_" + uuid4().hex
    maintenance = template_base_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(_database_url(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = template_base_url.set(database=name)
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(
            app_config.settings,
            # Alembic/SQLAlchemy must retain the installed psycopg v3 driver;
            # only direct psycopg.connect calls use the plain PostgreSQL URL.
            database_url=target.render_as_string(hide_password=False),
        ),
    )
    try:
        template_database.bootstrap_database_local_prerequisites(target)
        yield target
    finally:
        template_database.drop_database(template_base_url, name)


def _index_oid(connection: psycopg.Connection, spec: contract.IndexSpec) -> int:
    oid = connection.execute(
        "SELECT to_regclass(%s)::oid", (f"public.{spec.name}",)
    ).fetchone()
    assert oid is not None and oid[0] is not None
    return int(oid[0])


def _assert_exact_index(
    connection: psycopg.Connection, spec: contract.IndexSpec
) -> None:
    row = connection.execute(
        """
        SELECT c.relkind, tn.nspname, t.relname,
               i.indisvalid, i.indisready, i.indislive, i.indisunique,
               EXISTS (SELECT 1 FROM pg_constraint WHERE conindid=c.oid),
               am.amname, i.indnkeyatts, i.indnatts,
               i.indpred IS NOT NULL, i.indexprs IS NOT NULL,
               ARRAY(SELECT pg_get_indexdef(c.oid, p, true)
                     FROM generate_series(1, i.indnkeyatts) AS p),
               ARRAY(SELECT pg_index_column_has_property(c.oid, p, 'desc')
                     FROM generate_series(1, i.indnkeyatts) AS p),
               ARRAY(SELECT pg_index_column_has_property(c.oid, p, 'nulls_first')
                     FROM generate_series(1, i.indnkeyatts) AS p),
               EXISTS (SELECT 1 FROM pg_stat_progress_create_index
                       WHERE index_relid=c.oid)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid=c.relnamespace
        LEFT JOIN pg_index AS i ON i.indexrelid=c.oid
        LEFT JOIN pg_class AS t ON t.oid=i.indrelid
        LEFT JOIN pg_namespace AS tn ON tn.oid=t.relnamespace
        LEFT JOIN pg_am AS am ON am.oid=c.relam
        WHERE n.nspname='public' AND c.relname=%s
        """,
        (spec.name,),
    ).fetchone()
    assert row == (
        "i",
        "public",
        spec.table,
        True,
        True,
        True,
        False,
        False,
        "btree",
        4,
        4,
        False,
        False,
        list(spec.keys),
        [False] * 4,
        [False] * 4,
        False,
    )


def _assert_all_exact(connection: psycopg.Connection) -> None:
    for spec in contract.INDEX_SPECS:
        _assert_exact_index(connection, spec)


def test_fresh_chain_builds_both_exact_reporting_indexes(
    fresh_migration_database: URL,
) -> None:
    _upgrade("heads")
    with psycopg.connect(_database_url(fresh_migration_database)) as connection:
        _assert_all_exact(connection)


def test_fresh_migrated_head_passes_the_deployment_contract(db_session) -> None:
    contract.validate_postgres_indexes(db_session.connection())


@pytest.mark.parametrize("spec", contract.INDEX_SPECS)
def test_forward_migration_repairs_real_timeout_without_rebuilding_other_index(
    cloned_database: Callable[[str], URL],
    spec: contract.IndexSpec,
) -> None:
    url = cloned_database(PREDECESSOR)
    other = next(candidate for candidate in contract.INDEX_SPECS if candidate != spec)
    with psycopg.connect(_database_url(url), autocommit=True) as catalog:
        other_oid = _index_oid(catalog, other)
        counts = catalog.execute(
            "SELECT (SELECT count(*) FROM invoices), (SELECT count(*) FROM payments)"
        ).fetchone()
        catalog.execute(spec.drop_sql)
        with psycopg.connect(_database_url(url)) as blocker:
            # This writer lock permits the initial catalog entry, but delays
            # the concurrent build's first table scan.
            blocker.execute(
                sql.SQL("LOCK TABLE public.{} IN ROW EXCLUSIVE MODE").format(
                    sql.Identifier(spec.table)
                )
            )
            catalog.execute("SET lock_timeout = '200ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                catalog.execute(spec.create_sql)
            blocker.rollback()
        catalog.execute("RESET lock_timeout")
        # Reproduce migration 658's old retry: PostgreSQL reports success but
        # IF NOT EXISTS retains the interrupted, invalid catalog object.
        catalog.execute(
            spec.create_sql.replace("CONCURRENTLY", "CONCURRENTLY IF NOT EXISTS")
        )
        invalid = catalog.execute(
            """
            SELECT i.indisvalid, i.indisready
            FROM pg_index AS i
            WHERE i.indexrelid=to_regclass(%s)
            """,
            (f"public.{spec.name}",),
        ).fetchone()
        assert invalid is not None and invalid != (True, True)
        assert catalog.execute(
            "SELECT count(*) FROM alembic_version WHERE version_num=%s",
            (PREDECESSOR,),
        ).fetchone() == (1,)

    # Drive the real predecessor-to-repair Alembic upgrade. No helper call or
    # hand-stamped current metadata substitutes for the migration here.
    _upgrade(REPAIR)
    with psycopg.connect(_database_url(url)) as catalog:
        _assert_all_exact(catalog)
        assert _index_oid(catalog, other) == other_oid
        assert (
            catalog.execute(
                "SELECT (SELECT count(*) FROM invoices), (SELECT count(*) FROM payments)"
            ).fetchone()
            == counts
        )
        assert catalog.execute(
            "SELECT count(*) FROM alembic_version WHERE version_num=%s",
            (REPAIR,),
        ).fetchone() == (1,)
        oids = tuple(_index_oid(catalog, item) for item in contract.INDEX_SPECS)
    _upgrade(REPAIR)
    with psycopg.connect(_database_url(url)) as catalog:
        assert tuple(_index_oid(catalog, item) for item in contract.INDEX_SPECS) == oids


def test_forward_migration_restores_missing_indexes_and_downgrade_preserves_them(
    cloned_database: Callable[[str], URL],
) -> None:
    url = cloned_database(PREDECESSOR)
    with psycopg.connect(_database_url(url), autocommit=True) as catalog:
        for spec in contract.INDEX_SPECS:
            catalog.execute(spec.drop_sql)
    _upgrade(REPAIR)
    with psycopg.connect(_database_url(url)) as catalog:
        _assert_all_exact(catalog)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.downgrade(config, PREDECESSOR)
    with psycopg.connect(_database_url(url)) as catalog:
        _assert_all_exact(catalog)
