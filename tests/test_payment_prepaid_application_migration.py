from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa


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


def _legacy_connection(*, populated: bool):
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    metadata = sa.MetaData()
    legacy = sa.Table(
        "payment_prepaid_applications",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("payment_id", sa.String, nullable=False),
        sa.Column("settlement_id", sa.String, nullable=False),
        sa.Column("account_id", sa.String, nullable=False),
        sa.Column("subscription_id", sa.String, nullable=False),
        sa.Column("credit_ledger_entry_id", sa.String, nullable=False),
        sa.Column("debit_ledger_entry_id", sa.String, nullable=False),
        sa.Column("entitlement_id", sa.String, nullable=False),
        sa.Column("retired_allocation_id", sa.String, nullable=True),
        sa.Column("historical_invoice_id", sa.String, nullable=True),
        sa.Column("invoice_closure_id", sa.String, nullable=True),
        sa.Column("origin", sa.String, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String, nullable=False),
        sa.Column("period_start", sa.String, nullable=False),
        sa.Column("period_end", sa.String, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("preview_fingerprint", sa.String, nullable=False),
        sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("access_recheck_status", sa.String, nullable=False),
        sa.Column("access_recheck_error", sa.String, nullable=True),
        sa.Column("access_rechecked_at", sa.String, nullable=True),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    sa.Index(
        "uq_payment_prepaid_applications_idempotency_key",
        legacy.c.idempotency_key,
        unique=True,
    )
    metadata.create_all(connection)
    if populated:
        connection.execute(
            legacy.insert(),
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "payment_id": "00000000-0000-0000-0000-000000000002",
                "settlement_id": "00000000-0000-0000-0000-000000000003",
                "account_id": "00000000-0000-0000-0000-000000000004",
                "subscription_id": "00000000-0000-0000-0000-000000000005",
                "credit_ledger_entry_id": ("00000000-0000-0000-0000-000000000006"),
                "debit_ledger_entry_id": "00000000-0000-0000-0000-000000000007",
                "entitlement_id": "00000000-0000-0000-0000-000000000008",
                "retired_allocation_id": None,
                "historical_invoice_id": None,
                "invoice_closure_id": None,
                "origin": "post_settlement",
                "amount": Decimal("18812.50"),
                "currency": "NGN",
                "period_start": "2026-07-20T00:00:00+00:00",
                "period_end": "2026-08-20T00:00:00+00:00",
                "reason": "reviewed exact payment-funded period evidence",
                "preview_fingerprint": "a" * 64,
                "idempotency_key": "archive-migration-test",
                "access_recheck_status": "completed",
                "access_recheck_error": None,
                "access_rechecked_at": "2026-07-20T00:01:00+00:00",
                "created_at": "2026-07-20T00:00:00+00:00",
                "updated_at": "2026-07-20T00:01:00+00:00",
            },
        )
    return connection


def _run_retirement(monkeypatch, migration, connection) -> None:
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    def rename_table(source: str, target: str) -> None:
        assert source == "payment_prepaid_applications"
        assert target == "payment_prepaid_applications_archive"
        connection.exec_driver_sql(f'ALTER TABLE "{source}" RENAME TO "{target}"')

    monkeypatch.setattr(migration.op, "rename_table", rename_table)


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
    connection = _legacy_connection(populated=False)
    _run_retirement(monkeypatch, migration, connection)

    migration.upgrade()

    inspector = sa.inspect(connection)
    assert not inspector.has_table("payment_prepaid_applications")
    assert inspector.has_table("payment_prepaid_applications_archive")
    assert (
        connection.scalar(
            sa.text("SELECT COUNT(*) FROM payment_prepaid_applications_archive")
        )
        == 0
    )


def test_payment_prepaid_application_retirement_preserves_production_shaped_evidence(
    monkeypatch,
):
    migration = _load_retirement()
    connection = _legacy_connection(populated=True)
    _run_retirement(monkeypatch, migration, connection)

    migration.upgrade()

    inspector = sa.inspect(connection)
    assert not inspector.has_table("payment_prepaid_applications")
    assert inspector.has_table("payment_prepaid_applications_archive")
    archive = sa.Table(
        "payment_prepaid_applications_archive",
        sa.MetaData(),
        autoload_with=connection,
    )
    row = connection.execute(sa.select(archive)).mappings().one()
    assert set(row) == {
        "id",
        "payment_id",
        "settlement_id",
        "account_id",
        "subscription_id",
        "credit_ledger_entry_id",
        "debit_ledger_entry_id",
        "entitlement_id",
        "retired_allocation_id",
        "historical_invoice_id",
        "invoice_closure_id",
        "origin",
        "amount",
        "currency",
        "period_start",
        "period_end",
        "reason",
        "preview_fingerprint",
        "idempotency_key",
        "access_recheck_status",
        "access_recheck_error",
        "access_rechecked_at",
        "created_at",
        "updated_at",
    }
    assert row["id"] == "00000000-0000-0000-0000-000000000001"
    assert row["amount"] == Decimal("18812.50")
    assert row["origin"] == "post_settlement"
    assert row["access_recheck_status"] == "completed"
    assert row["preview_fingerprint"] == "a" * 64
    assert {index["name"] for index in inspector.get_indexes(archive.name)} == {
        "uq_payment_prepaid_applications_idempotency_key"
    }


def test_payment_prepaid_application_retirement_fails_on_ambiguous_tables(
    monkeypatch,
):
    migration = _load_retirement()
    connection = _legacy_connection(populated=True)
    connection.exec_driver_sql(
        "CREATE TABLE payment_prepaid_applications_archive (id TEXT PRIMARY KEY)"
    )
    _run_retirement(monkeypatch, migration, connection)

    with pytest.raises(RuntimeError, match="both .* exist"):
        migration.upgrade()


def test_payment_prepaid_application_retirement_is_idempotent_with_archive(
    monkeypatch,
):
    migration = _load_retirement()
    bind = MagicMock()
    rename_table = MagicMock()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration,
        "_has_table",
        lambda _bind, table_name: table_name == migration._ARCHIVE_TABLE,
    )
    monkeypatch.setattr(migration.op, "rename_table", rename_table)

    migration.upgrade()

    rename_table.assert_not_called()
    bind.scalar.assert_not_called()


def test_archive_compatibility_revision_creates_the_complete_empty_shape(monkeypatch):
    migration = _load_archive_compatibility()
    bind = MagicMock()
    create_table = MagicMock()
    create_index = MagicMock()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration, "_has_table", lambda *_args: False)
    monkeypatch.setattr(migration.op, "create_table", create_table)
    monkeypatch.setattr(migration.op, "create_index", create_index)

    migration.upgrade()

    table_args = create_table.call_args.args
    assert table_args[0] == "payment_prepaid_applications_archive"
    assert {arg.name for arg in table_args[1:] if isinstance(arg, sa.Column)} == {
        "id",
        "payment_id",
        "settlement_id",
        "account_id",
        "subscription_id",
        "credit_ledger_entry_id",
        "debit_ledger_entry_id",
        "entitlement_id",
        "retired_allocation_id",
        "historical_invoice_id",
        "invoice_closure_id",
        "origin",
        "amount",
        "currency",
        "period_start",
        "period_end",
        "reason",
        "preview_fingerprint",
        "idempotency_key",
        "access_recheck_status",
        "access_recheck_error",
        "access_rechecked_at",
        "created_at",
        "updated_at",
    }
    assert {
        element.target_fullname
        for arg in table_args[1:]
        if isinstance(arg, sa.ForeignKeyConstraint)
        for element in arg.elements
    } == {
        "payments.id",
        "payment_settlements.id",
        "subscribers.id",
        "subscriptions.id",
        "ledger_entries.id",
        "service_entitlements.id",
        "payment_allocations.id",
        "invoices.id",
        "invoice_closures.id",
    }
    assert {
        arg.name for arg in table_args[1:] if isinstance(arg, sa.CheckConstraint)
    } == {
        "ck_payment_prepaid_applications_amount_positive",
        "ck_payment_prepaid_applications_period_order",
        "ck_payment_prepaid_applications_origin",
        "ck_payment_prepaid_applications_access_status",
    }
    assert create_index.call_count == 8
    assert {call.args[2][0] for call in create_index.call_args_list} == {
        "payment_id",
        "settlement_id",
        "credit_ledger_entry_id",
        "debit_ledger_entry_id",
        "entitlement_id",
        "retired_allocation_id",
        "invoice_closure_id",
        "idempotency_key",
    }


def test_archive_compatibility_revision_is_idempotent_when_archive_exists(
    monkeypatch,
):
    migration = _load_archive_compatibility()
    create_table = MagicMock()
    monkeypatch.setattr(migration.op, "get_bind", MagicMock())
    monkeypatch.setattr(
        migration,
        "_has_table",
        lambda _bind, table_name: table_name == migration._ARCHIVE_TABLE,
    )
    monkeypatch.setattr(migration.op, "create_table", create_table)

    migration.upgrade()

    create_table.assert_not_called()


@pytest.mark.parametrize("archive_exists", [False, True])
def test_archive_compatibility_revision_fails_if_legacy_table_remains(
    monkeypatch,
    archive_exists,
):
    migration = _load_archive_compatibility()
    create_table = MagicMock()
    monkeypatch.setattr(migration.op, "get_bind", MagicMock())
    monkeypatch.setattr(
        migration,
        "_has_table",
        lambda _bind, table_name: (
            table_name == migration._LEGACY_TABLE or archive_exists
        ),
    )
    monkeypatch.setattr(migration.op, "create_table", create_table)

    with pytest.raises(RuntimeError, match="still exists"):
        migration.upgrade()

    create_table.assert_not_called()
