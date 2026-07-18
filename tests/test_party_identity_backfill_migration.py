from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/351_party_identity_backfill_receipts.py"
    )
    spec = importlib.util.spec_from_file_location("migration_348", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_receipt_revision_is_linear_and_schema_only():
    migration = _load_migration()

    assert migration.revision == "351_party_identity_backfill_receipts"
    assert migration.down_revision == "350_subscriber_party_binding"
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert '"party_identity_backfill_receipts"' in source
    assert '"plan_digest"' in source
    assert '"approval_file_sha256"' in source
    assert '"manifest"' in source
    assert "uq_party_backfill_receipts_plan_digest" in source
    assert "ck_party_backfill_receipts_digest_lengths" in source
    assert "op.execute" not in source
    assert "op.bulk_insert" not in source


def test_backfill_receipt_migration_contains_no_identity_values():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    for prohibited in (
        "display_name",
        "email",
        "phone",
        "nin",
        'approved_by"',
        'approval_reason"',
    ):
        assert prohibited not in source
