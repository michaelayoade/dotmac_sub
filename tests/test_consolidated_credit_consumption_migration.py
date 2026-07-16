from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/320_consolidated_credit_consumption_reconciliation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_320", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_consolidated_credit_consumption_revision_is_linear_and_structural():
    migration = _load_migration()

    assert migration.revision == "320_consolidated_credit_consumption_reconciliation"
    assert migration.down_revision == "319_device_projection_table"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "consolidated_credit_consumption_reconciliation_evidence" in source
    assert "billing_account_credit_allocations.id" in source
    assert "ck_consolidated_credit_recon_debit_action" in source
    assert "uq_consolidated_credit_recon_allocation" in source
