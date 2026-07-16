from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/325_consolidated_return_document_reconstruction.py"
    )
    spec = importlib.util.spec_from_file_location("migration_325", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_consolidated_return_document_revision_is_linear_and_structural():
    migration = _load_migration()

    assert migration.revision == "325_consolidated_return_document_reconstruction"
    assert migration.down_revision == "324_consolidated_return_reconciliation"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "consolidated_payment_return_document_reconstruction_evidence" in source
    assert "consolidated_payment_return_reconciliation_evidence.id" in source
    assert "historical_payment_state" in source
    assert "source_reference" in source
    assert "preview_fingerprint" in source
    assert "uq_consolidated_return_document_recon_evidence" in source
