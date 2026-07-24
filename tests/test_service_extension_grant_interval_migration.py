"""Migration coverage for exact service-extension grant intervals."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/417_service_extension_grant_intervals.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_417_service_extension_grant_intervals",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entries_table(engine: sa.Engine) -> sa.Table:
    metadata = sa.MetaData()
    entries = sa.Table(
        "service_extension_entries",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("extension_id", sa.String(36), nullable=False),
        sa.Column("subscription_id", sa.String(36), nullable=False),
        sa.Column("subscriber_id", sa.String(36), nullable=False),
        sa.Column("previous_next_billing_at", sa.DateTime(timezone=True)),
        sa.Column("new_next_billing_at", sa.DateTime(timezone=True)),
    )
    metadata.create_all(engine)
    return entries


def test_migration_backfills_legacy_interval_without_reinterpreting_it() -> None:
    engine = sa.create_engine("sqlite://")
    entries = _entries_table(engine)
    previous = datetime(2026, 7, 2, tzinfo=UTC)
    historical_end = datetime(2026, 7, 12, tzinfo=UTC)

    with engine.begin() as connection:
        connection.execute(
            entries.insert().values(
                id="entry-1",
                extension_id="extension-1",
                subscription_id="subscription-1",
                subscriber_id="subscriber-1",
                previous_next_billing_at=previous,
                new_next_billing_at=historical_end,
            )
        )
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        row = connection.execute(
            sa.text(
                """
                SELECT grant_starts_at, grant_ends_at, anchor_basis
                FROM service_extension_entries
                WHERE id = 'entry-1'
                """
            )
        ).one()
        indexes = {
            item["name"]
            for item in sa.inspect(connection).get_indexes("service_extension_entries")
        }

    assert datetime.fromisoformat(row.grant_starts_at) == previous.replace(tzinfo=None)
    assert datetime.fromisoformat(row.grant_ends_at) == historical_end.replace(
        tzinfo=None
    )
    assert row.anchor_basis == "legacy_previous_anchor"
    assert "ix_service_extension_entries_subscriber_grant_end" in indexes
    assert "uq_service_extension_entries_extension_subscription" in indexes


def test_migration_refuses_historical_duplicate_entry_identity() -> None:
    engine = sa.create_engine("sqlite://")
    entries = _entries_table(engine)
    values = {
        "extension_id": "extension-1",
        "subscription_id": "subscription-1",
        "subscriber_id": "subscriber-1",
        "previous_next_billing_at": datetime(2026, 7, 2, tzinfo=UTC),
        "new_next_billing_at": datetime(2026, 7, 12, tzinfo=UTC),
    }

    with engine.begin() as connection:
        connection.execute(
            entries.insert(),
            [{"id": "entry-1", **values}, {"id": "entry-2", **values}],
        )
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))

        with pytest.raises(RuntimeError, match="reviewed reconciliation"):
            migration.upgrade()
