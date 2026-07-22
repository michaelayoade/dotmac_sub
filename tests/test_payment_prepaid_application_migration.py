from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from scripts.migration.payment_prepaid_application_archive_schema import (
    ARCHIVE_TABLE,
    INDEX_CONTRACTS,
    LEGACY_TABLE,
    archive_table_elements,
    validate_archive_schema,
)


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/357_payment_prepaid_applications.py"
    )
    spec = importlib.util.spec_from_file_location("migration_357", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_retirement():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/394_retire_payment_prepaid_applications.py"
    )
    spec = importlib.util.spec_from_file_location("migration_394", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_archive_compatibility():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/396_payment_prepaid_application_archive.py"
    )
    spec = importlib.util.spec_from_file_location("migration_396", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_forward_validation():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/397_validate_payment_prepaid_application_archive.py"
    )
    spec = importlib.util.spec_from_file_location("migration_397", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REFERENCE_TABLES = (
    "payments",
    "payment_settlements",
    "subscribers",
    "subscriptions",
    "ledger_entries",
    "service_entitlements",
    "payment_allocations",
    "invoices",
    "invoice_closures",
)


def _connection_with_references():
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    metadata = sa.MetaData()
    for table_name in _REFERENCE_TABLES:
        sa.Table(table_name, metadata, sa.Column("id", sa.String, primary_key=True))
    metadata.create_all(connection)
    return connection


def _create_complete_table(connection, table_name: str, *, populated: bool) -> None:
    metadata = sa.MetaData()
    for reference_table in _REFERENCE_TABLES:
        sa.Table(
            reference_table,
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            extend_existing=True,
        )
    table = sa.Table(table_name, metadata, *archive_table_elements())
    table.create(connection)
    for index_name, columns, unique in INDEX_CONTRACTS:
        sa.Index(
            index_name,
            *(table.c[column] for column in columns),
            unique=unique,
        ).create(connection)
    if populated:
        reference_ids = {
            "payments": UUID("00000000-0000-0000-0000-000000000002"),
            "payment_settlements": UUID("00000000-0000-0000-0000-000000000003"),
            "subscribers": UUID("00000000-0000-0000-0000-000000000004"),
            "subscriptions": UUID("00000000-0000-0000-0000-000000000005"),
            "ledger_entries": UUID("00000000-0000-0000-0000-000000000006"),
            "service_entitlements": UUID("00000000-0000-0000-0000-000000000008"),
        }
        for reference_table, identifier in reference_ids.items():
            reference = sa.Table(
                reference_table, sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                reference.insert(),
                {"id": str(identifier)},
            )
        ledger_entries = sa.Table(
            "ledger_entries", sa.MetaData(), autoload_with=connection
        )
        connection.execute(
            ledger_entries.insert(),
            {"id": "00000000-0000-0000-0000-000000000007"},
        )
        connection.execute(
            table.insert(),
            {
                "id": UUID("00000000-0000-0000-0000-000000000001"),
                "payment_id": reference_ids["payments"],
                "settlement_id": reference_ids["payment_settlements"],
                "account_id": reference_ids["subscribers"],
                "subscription_id": reference_ids["subscriptions"],
                "credit_ledger_entry_id": reference_ids["ledger_entries"],
                "debit_ledger_entry_id": UUID("00000000-0000-0000-0000-000000000007"),
                "entitlement_id": reference_ids["service_entitlements"],
                "retired_allocation_id": None,
                "historical_invoice_id": None,
                "invoice_closure_id": None,
                "origin": "post_settlement",
                "amount": Decimal("18812.50"),
                "currency": "NGN",
                "period_start": datetime(2026, 7, 20, tzinfo=UTC),
                "period_end": datetime(2026, 8, 20, tzinfo=UTC),
                "reason": "reviewed exact payment-funded period evidence",
                "preview_fingerprint": "a" * 64,
                "idempotency_key": "archive-migration-test",
                "access_recheck_status": "completed",
                "access_recheck_error": None,
                "access_rechecked_at": datetime(2026, 7, 20, 0, 1, tzinfo=UTC),
                "created_at": datetime(2026, 7, 20, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 20, 0, 1, tzinfo=UTC),
            },
        )


def _connection_with_table(table_name: str, *, populated: bool):
    connection = _connection_with_references()
    _create_complete_table(connection, table_name, populated=populated)
    return connection


def _run_migration(monkeypatch, migration, connection) -> None:
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))


def test_payment_prepaid_application_revision_is_linear_and_structural():
    migration = _load_migration()

    assert migration.revision == "359_payment_prepaid_applications"
    assert migration.down_revision == "358_paystack_allocation_exceptions"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "payment_prepaid_applications" in source
    assert "payment_settlements.id" in source
    assert "service_entitlements.id" in source
    assert "payment_allocations.id" in source
    assert "invoice_closures.id" in source
    assert "ck_payment_prepaid_applications_period_order" in source


def test_payment_prepaid_application_retirement_archives_an_empty_table(monkeypatch):
    migration = _load_retirement()
    connection = _connection_with_table(LEGACY_TABLE, populated=False)
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    inspector = sa.inspect(connection)
    assert not inspector.has_table(LEGACY_TABLE)
    assert inspector.has_table(ARCHIVE_TABLE)
    assert validate_archive_schema(connection, expected_row_count=0) == 0


def test_payment_prepaid_application_retirement_preserves_production_shaped_evidence(
    monkeypatch,
):
    migration = _load_retirement()
    connection = _connection_with_table(LEGACY_TABLE, populated=True)
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    inspector = sa.inspect(connection)
    assert not inspector.has_table(LEGACY_TABLE)
    assert inspector.has_table(ARCHIVE_TABLE)
    assert validate_archive_schema(connection, expected_row_count=1) == 1
    archive = sa.Table(ARCHIVE_TABLE, sa.MetaData(), autoload_with=connection)
    row = (
        connection.execute(
            sa.select(
                archive.c.amount,
                archive.c.origin,
                archive.c.access_recheck_status,
                archive.c.preview_fingerprint,
            )
        )
        .mappings()
        .one()
    )
    assert Decimal(str(row["amount"])) == Decimal("18812.50")
    assert row["origin"] == "post_settlement"
    assert row["access_recheck_status"] == "completed"
    assert row["preview_fingerprint"] == "a" * 64


def test_payment_prepaid_application_retirement_fails_on_ambiguous_tables(
    monkeypatch,
):
    migration = _load_retirement()
    connection = _connection_with_table(LEGACY_TABLE, populated=True)
    connection.exec_driver_sql(f"CREATE TABLE {ARCHIVE_TABLE} (id TEXT PRIMARY KEY)")
    _run_migration(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError, match="both .* exist"):
        migration.upgrade()


def test_payment_prepaid_application_retirement_rejects_neither_table(monkeypatch):
    migration = _load_retirement()
    connection = _connection_with_references()
    _run_migration(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError, match="neither .* nor .* exists"):
        migration.upgrade()


def test_payment_prepaid_application_retirement_accepts_verified_archive_only(
    monkeypatch,
):
    migration = _load_retirement()
    connection = _connection_with_table(ARCHIVE_TABLE, populated=True)
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    assert validate_archive_schema(connection, expected_row_count=1) == 1


def test_payment_prepaid_application_retirement_rejects_malformed_archive_only(
    monkeypatch,
):
    migration = _load_retirement()
    connection = _connection_with_references()
    connection.exec_driver_sql(f"CREATE TABLE {ARCHIVE_TABLE} (id TEXT PRIMARY KEY)")
    _run_migration(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError, match="schema validation failed"):
        migration.upgrade()


def test_archive_compatibility_revision_creates_the_complete_empty_shape(monkeypatch):
    migration = _load_archive_compatibility()
    connection = _connection_with_references()
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    assert validate_archive_schema(connection, expected_row_count=0) == 0


def test_archive_compatibility_revision_validates_populated_archive(
    monkeypatch,
):
    migration = _load_archive_compatibility()
    connection = _connection_with_table(ARCHIVE_TABLE, populated=True)
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    assert validate_archive_schema(connection, expected_row_count=1) == 1


def test_archive_compatibility_revision_rejects_malformed_archive(monkeypatch):
    migration = _load_archive_compatibility()
    connection = _connection_with_references()
    connection.exec_driver_sql(f"CREATE TABLE {ARCHIVE_TABLE} (id TEXT PRIMARY KEY)")
    _run_migration(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError, match="schema validation failed"):
        migration.upgrade()


def test_archive_compatibility_revision_rejects_incomplete_archive_index_contract(
    monkeypatch,
):
    migration = _load_archive_compatibility()
    connection = _connection_with_table(ARCHIVE_TABLE, populated=False)
    connection.exec_driver_sql("DROP INDEX uq_payment_prepaid_applications_payment_id")
    _run_migration(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError, match="index mismatch"):
        migration.upgrade()


@pytest.mark.parametrize("archive_exists", [False, True])
def test_archive_compatibility_revision_fails_if_legacy_table_remains(
    monkeypatch,
    archive_exists,
):
    migration = _load_archive_compatibility()
    connection = _connection_with_table(LEGACY_TABLE, populated=False)
    if archive_exists:
        connection.exec_driver_sql(
            f"CREATE TABLE {ARCHIVE_TABLE} (id TEXT PRIMARY KEY)"
        )
    _run_migration(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError, match="still exists"):
        migration.upgrade()


@pytest.mark.parametrize("state", ["missing", "legacy", "both", "malformed"])
def test_forward_validation_rejects_invalid_existing_396_state(monkeypatch, state):
    migration = _load_forward_validation()
    connection = _connection_with_references()
    if state in {"legacy", "both"}:
        _create_complete_table(connection, LEGACY_TABLE, populated=False)
    if state == "both":
        connection.exec_driver_sql(
            f"CREATE TABLE {ARCHIVE_TABLE} (id TEXT PRIMARY KEY)"
        )
    if state == "malformed":
        connection.exec_driver_sql(
            f"CREATE TABLE {ARCHIVE_TABLE} (id TEXT PRIMARY KEY)"
        )
    _run_migration(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError):
        migration.upgrade()


def test_forward_validation_accepts_complete_populated_archive(monkeypatch):
    migration = _load_forward_validation()
    connection = _connection_with_table(ARCHIVE_TABLE, populated=True)
    _run_migration(monkeypatch, migration, connection)

    migration.upgrade()

    assert validate_archive_schema(connection, expected_row_count=1) == 1


def test_archive_is_excluded_from_alembic_autogenerate():
    source = (Path(__file__).resolve().parents[1] / "alembic/env.py").read_text(
        encoding="utf-8"
    )

    assert '"payment_prepaid_applications_archive"' in source
