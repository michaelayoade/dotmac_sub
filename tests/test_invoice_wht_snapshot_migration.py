from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/426_invoice_withholding_tax_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location("migration_426", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure_operations(monkeypatch, migration, connection) -> None:
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))


def test_invoice_wht_snapshot_migration_upgrades_and_downgrades_sqlite(monkeypatch):
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    sa.Table(
        "invoices",
        sa.MetaData(),
        sa.Column("id", sa.String, primary_key=True),
    ).create(connection)
    _configure_operations(monkeypatch, migration, connection)

    migration.upgrade()

    columns = {
        column["name"] for column in sa.inspect(connection).get_columns("invoices")
    }
    assert {
        "withholding_tax_rate",
        "withholding_tax_amount",
        "withholding_tax_taxable_basis",
        "bank_transfer_net_payable",
        "withholding_tax_policy_enabled",
        "withholding_tax_policy_version",
    } <= columns
    checks = {
        check["name"] for check in sa.inspect(connection).get_check_constraints("invoices")
    }
    assert "ck_invoices_wht_snapshot_rate_range" in checks

    connection.execute(
        sa.text(
            "INSERT INTO invoices (id, withholding_tax_policy_enabled, "
            "withholding_tax_taxable_basis, bank_transfer_net_payable) "
            "VALUES ('snapshot', 0, 100.00, 100.00)"
        )
    )
    with pytest.raises(RuntimeError, match="immutable invoice withholding-tax snapshots"):
        migration.downgrade()
    connection.execute(sa.text("DELETE FROM invoices WHERE id = 'snapshot'"))
    migration.downgrade()

    columns = {
        column["name"] for column in sa.inspect(connection).get_columns("invoices")
    }
    assert columns == {"id"}
