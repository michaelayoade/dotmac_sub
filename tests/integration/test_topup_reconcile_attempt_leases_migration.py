"""PostgreSQL 562-to-563 proof for durable reconciliation attempt leases."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import URL, make_url

from alembic import command
from app import config as app_config

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "562_topup_reconcile_progress"
CANDIDATE = "563_topup_reconcile_leases"
DUE_INDEX_NAME = "ix_topup_intents_gateway_reconcile_due"
PROVIDER_ATTEMPT_INDEX_NAME = "ix_topup_intents_provider_reconcile_attempt"
ATTEMPT_COLUMNS = {
    "gateway_last_reconcile_attempt_at",
    "gateway_reconcile_attempt_count",
}


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def predecessor_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError("migration-path test requires TEST_DATABASE_URL")
    base = make_url(configured)
    if not base.drivername.startswith("postgresql"):
        raise pytest.UsageError("migration-path test requires PostgreSQL")

    database_name = f"dotmac_topup_reconcile_migration_{uuid4().hex}"
    maintenance = base.set(database="postgres")
    with psycopg.connect(_render(maintenance), autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    target = base.set(database=database_name)
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(
            app_config.settings,
            database_url=target.render_as_string(hide_password=False),
        ),
    )
    try:
        yield target
    finally:
        with psycopg.connect(_render(maintenance), autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def test_revision_563_upgrades_an_already_applied_562_database(
    predecessor_database: URL,
) -> None:
    _upgrade(PREDECESSOR)
    malformed_id = uuid4()
    valid_id = uuid4()
    created_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    with psycopg.connect(_render(predecessor_database)) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO topup_intents "
                "(id, reference, provider_type, currency, requested_amount, status, "
                '"metadata", created_at, updated_at) '
                "VALUES (%s, %s, %s, 'NGN', %s, %s, %s, %s, %s)",
                (
                    (
                        malformed_id,
                        "DMAC-MIGRATION-MALFORMED",
                        "paystack",
                        Decimal("5000.00"),
                        "failed",
                        Jsonb(
                            {
                                "gateway_verification": {
                                    "observed_at": "2026-99-99Tbroken",
                                    "outcome": "unavailable",
                                    "reason_code": "provider_unavailable",
                                }
                            }
                        ),
                        created_at,
                        created_at,
                    ),
                    (
                        valid_id,
                        "DMAC-MIGRATION-VALID",
                        "flutterwave",
                        Decimal("3000.00"),
                        "canceled",
                        Jsonb(
                            {
                                "gateway_verification": {
                                    "observed_at": "2026-08-20T09:00:00+00:00",
                                    "outcome": "failed",
                                    "reason_code": "provider_reported_failed",
                                }
                            }
                        ),
                        created_at,
                        created_at,
                    ),
                ),
            )
        connection.commit()

    _upgrade(CANDIDATE)

    with psycopg.connect(_render(predecessor_database)) as connection:
        columns = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'topup_intents'"
            )
        }
        index_definitions = dict(
            connection.execute(
                "SELECT indexes.indexname, indexes.indexdef "
                "FROM pg_indexes AS indexes "
                "JOIN pg_class AS relation ON relation.relname = indexes.indexname "
                "JOIN pg_index AS catalog ON catalog.indexrelid = relation.oid "
                "WHERE indexes.schemaname = 'public' "
                "AND indexes.tablename = 'topup_intents' "
                "AND indexes.indexname IN (%s, %s) AND catalog.indisvalid",
                (DUE_INDEX_NAME, PROVIDER_ATTEMPT_INDEX_NAME),
            ).fetchall()
        )
        rows = connection.execute(
            "SELECT id, gateway_last_reconcile_attempt_at, "
            "gateway_reconcile_attempt_count FROM topup_intents WHERE id IN (%s, %s) "
            "ORDER BY reference",
            (malformed_id, valid_id),
        ).fetchall()

    assert ATTEMPT_COLUMNS <= columns.keys()
    assert columns["gateway_reconcile_attempt_count"] == "NO"
    assert set(index_definitions) == {DUE_INDEX_NAME, PROVIDER_ATTEMPT_INDEX_NAME}
    normalized_due_index = " ".join(index_definitions[DUE_INDEX_NAME].split())
    assert "gateway_last_reconcile_attempt_at" in normalized_due_index
    assert "WHERE ((completed_payment_id IS NULL)" in normalized_due_index
    assert "'canceled'" in normalized_due_index
    normalized_provider_index = " ".join(
        index_definitions[PROVIDER_ATTEMPT_INDEX_NAME].split()
    )
    assert "(provider_type, gateway_last_reconcile_attempt_at)" in (
        normalized_provider_index
    )
    assert "WHERE (gateway_last_reconcile_attempt_at IS NOT NULL)" in (
        normalized_provider_index
    )

    assert len(rows) == 2
    for row in rows:
        assert row[1] is None
        assert row[2] == 0
