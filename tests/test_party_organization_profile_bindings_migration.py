from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/352_party_organization_profile_bindings.py"
    )
    spec = importlib.util.spec_from_file_location("migration_349", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_organization_profile_binding_revision_is_linear_and_schema_only():
    migration = _load_migration()

    assert migration.revision == "352_party_organization_profile_bindings"
    assert migration.down_revision == "351_party_identity_backfill_receipts"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    for table_name in ("organizations", "resellers", "vendors", "field_vendors"):
        assert f'"{table_name}"' in source
    for column_name in (
        "party_id",
        "party_bound_at",
        "party_binding_source",
        "party_binding_reason",
    ):
        assert f'"{column_name}"' in source
    assert "create_unique_constraint" in source
    assert "party_binding_evidence" in source
    assert "op.execute" not in source
    assert "op.bulk_insert" not in source


def test_migration_does_not_assign_roles_or_rewrite_legacy_classification():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert "party_roles" not in source
    assert "account_type" not in source
    assert "crm_vendor_id ==" not in source
