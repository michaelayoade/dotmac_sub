from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/324_consolidated_return_reconciliation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_324", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_consolidated_return_reconciliation_revision_is_linear_and_structural():
    migration = _load_migration()

    assert migration.revision == "324_consolidated_return_reconciliation"
    assert migration.down_revision == (
        "323_consolidated_credit_consumption_reconciliation"
    )
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "consolidated_payment_return_reconciliation_evidence" in source
    assert "payment_refunds.id" in source
    assert "payment_reversals.id" in source
    assert "ck_consolidated_return_recon_exactly_one_owner" in source
    assert "uq_consolidated_return_recon_refund" in source
    assert "uq_consolidated_return_recon_reversal" in source
