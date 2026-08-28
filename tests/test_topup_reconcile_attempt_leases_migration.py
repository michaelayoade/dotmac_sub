"""Migration 563 appends durable attempt leases to revision 562."""

from __future__ import annotations

import importlib.util
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/563_topup_reconcile_attempt_leases.py"

_ATTEMPT_COLUMNS = {
    "gateway_last_reconcile_attempt_at",
    "gateway_reconcile_attempt_count",
}
_DUE_INDEX = "ix_topup_intents_gateway_reconcile_due"
_PROVIDER_ATTEMPT_INDEX = "ix_topup_intents_provider_reconcile_attempt"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_563_topup_reconcile_attempt_leases",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_revision_562_topup_intents(connection: sa.Connection) -> sa.Table:
    metadata = sa.MetaData()
    table = sa.Table(
        "topup_intents",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("completed_payment_id", sa.String(36)),
        sa.Column("metadata", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("gateway_last_observed_at", sa.DateTime),
        sa.Column("gateway_last_outcome", sa.String(40)),
        sa.Column("gateway_last_reason_code", sa.String(80)),
        sa.Column("gateway_next_reconcile_at", sa.DateTime),
        sa.Column(
            "gateway_observation_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    metadata.create_all(connection)
    sa.Index(
        _DUE_INDEX,
        table.c.provider_type,
        table.c.status,
        table.c.completed_payment_id,
        table.c.gateway_next_reconcile_at,
        table.c.created_at,
    ).create(connection)
    return table


def test_revision_563_extends_published_562_and_requires_its_table() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            migration = _load_migration()
            migration.op = Operations(MigrationContext.configure(connection))

            assert migration.revision == "563_topup_reconcile_leases"
            assert migration.down_revision == "562_topup_reconcile_progress"
            with pytest.raises(
                RuntimeError,
                match="revision 563 requires the topup_intents table",
            ):
                migration.upgrade()
    finally:
        engine.dispose()


def test_revision_563_adds_attempts_and_replaces_the_562_due_index() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            table = _create_revision_562_topup_intents(connection)
            connection.execute(
                table.insert(),
                {
                    "id": "malformed-evidence",
                    "provider_type": "paystack",
                    "status": "failed",
                    "metadata": "not-json",
                    "created_at": datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
                },
            )
            migration = _load_migration()
            migration.op = Operations(MigrationContext.configure(connection))

            migration.upgrade()

            inspector = sa.inspect(connection)
            columns = {
                column["name"]: column
                for column in inspector.get_columns("topup_intents")
            }
            indexes = {
                index["name"]: index for index in inspector.get_indexes("topup_intents")
            }
            row = connection.execute(
                sa.text(
                    "SELECT gateway_last_reconcile_attempt_at, "
                    "gateway_reconcile_attempt_count FROM topup_intents"
                )
            ).one()
            index_sql = connection.scalar(
                sa.text(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND name = :name"
                ),
                {"name": _DUE_INDEX},
            )
            provider_index_sql = connection.scalar(
                sa.text(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND name = :name"
                ),
                {"name": _PROVIDER_ATTEMPT_INDEX},
            )
    finally:
        engine.dispose()

    assert _ATTEMPT_COLUMNS <= columns.keys()
    assert columns["gateway_reconcile_attempt_count"]["nullable"] is False
    assert row == (None, 0)
    assert indexes[_DUE_INDEX]["column_names"] == [
        "provider_type",
        "status",
        "gateway_next_reconcile_at",
        "gateway_last_reconcile_attempt_at",
        "created_at",
    ]
    assert indexes[_PROVIDER_ATTEMPT_INDEX]["column_names"] == [
        "provider_type",
        "gateway_last_reconcile_attempt_at",
    ]
    assert index_sql is not None
    normalized_index_sql = " ".join(index_sql.split())
    assert "WHERE completed_payment_id IS NULL" in normalized_index_sql
    assert "'pending', 'failed', 'abandoned', 'canceled', 'expired'" in (
        normalized_index_sql
    )
    assert provider_index_sql is not None
    assert "WHERE gateway_last_reconcile_attempt_at IS NOT NULL" in (
        " ".join(provider_index_sql.split())
    )


def test_revision_563_replaces_invalid_concurrent_index_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    expected_definition = (
        "CREATE INDEX ix_topup_intents_gateway_reconcile_due ON public.topup_intents "
        "USING btree (provider_type, status, gateway_next_reconcile_at, "
        "gateway_last_reconcile_attempt_at, created_at) WHERE "
        "((completed_payment_id IS NULL) AND ((status)::text = ANY "
        "((ARRAY['pending'::character varying, 'failed'::character varying, "
        "'abandoned'::character varying, 'canceled'::character varying, "
        "'expired'::character varying])::text[])))"
    )
    states = iter(((False, expected_definition), (True, expected_definition)))
    statements: list[str] = []
    monkeypatch.setattr(
        migration,
        "_postgresql_index_state",
        lambda _bind, _index_name: next(states),
    )
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=nullcontext),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration._ensure_postgresql_index(
        object(),
        index_name=migration._DUE_INDEX,
        create_sql=(
            f"CREATE INDEX CONCURRENTLY {migration._DUE_INDEX} ON topup_intents "
            f"({', '.join(migration._DUE_INDEX_COLUMNS)}) "
            f"WHERE {migration._DUE_INDEX_PREDICATE}"
        ),
        columns=migration._DUE_INDEX_COLUMNS,
        predicate_fragments=(
            "completed_payment_id is null",
            "pending",
            "failed",
            "abandoned",
            "canceled",
            "expired",
        ),
    )

    assert statements[0] == (
        "DROP INDEX CONCURRENTLY IF EXISTS ix_topup_intents_gateway_reconcile_due"
    )
    assert statements[1].startswith(
        "CREATE INDEX CONCURRENTLY ix_topup_intents_gateway_reconcile_due"
    )
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" not in statements[1]
