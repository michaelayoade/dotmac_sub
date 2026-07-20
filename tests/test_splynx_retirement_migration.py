"""Migration 330 retires only empty residue and preserves populated evidence."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "330_retire_splynx_import_archive.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "splynx_retirement_migration", _MIGRATION_PATH
)
assert _SPEC and _SPEC.loader
retirement = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retirement)


def _connection():
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    metadata = sa.MetaData()
    for table_name in (*retirement._PRESERVED_TABLES, *retirement._RETIRED_TABLES):
        sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
        )
    metadata.create_all(connection)
    return connection, metadata


def test_retirement_targets_only_proven_empty_tables() -> None:
    assert retirement._PRESERVED_TABLES == (
        "splynx_archived_ticket_messages",
        "splynx_archived_tickets",
        "splynx_id_mappings",
    )
    assert retirement._RETIRED_TABLES == (
        "splynx_archived_quote_items",
        "splynx_archived_quotes",
        "portal_onboarding_states",
    )


def test_upgrade_ignores_populated_preserved_evidence(monkeypatch) -> None:
    connection, metadata = _connection()
    for table_name in retirement._PRESERVED_TABLES:
        connection.execute(metadata.tables[table_name].insert(), {"id": 1})

    dropped: list[str] = []
    monkeypatch.setattr(retirement.op, "get_bind", lambda: connection)
    monkeypatch.setattr(retirement.op, "drop_table", dropped.append)

    retirement.upgrade()

    assert dropped == list(retirement._RETIRED_TABLES)
    assert not set(dropped).intersection(retirement._PRESERVED_TABLES)


def test_upgrade_blocks_nonempty_retirement_target() -> None:
    connection, metadata = _connection()
    quotes = metadata.tables["splynx_archived_quotes"]
    connection.execute(quotes.insert(), {"id": 1})

    with pytest.raises(RuntimeError, match="splynx_archived_quotes=1"):
        retirement._assert_safe_cutover(connection)


def test_downgrade_recreates_only_retired_tables(monkeypatch) -> None:
    created: list[str] = []

    def record_create(table_name, *_columns, **_kwargs):
        created.append(table_name)

    monkeypatch.setattr(retirement.op, "create_table", record_create)

    retirement.downgrade()

    assert created == [
        "splynx_archived_quotes",
        "splynx_archived_quote_items",
        "portal_onboarding_states",
    ]
    assert not set(created).intersection(retirement._PRESERVED_TABLES)
