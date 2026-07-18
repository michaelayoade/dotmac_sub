from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/350_subscriber_party_binding.py"
    )
    spec = importlib.util.spec_from_file_location("migration_347", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_subscriber_party_binding_revision_is_linear_and_schema_only():
    migration = _load_migration()

    assert migration.revision == "350_subscriber_party_binding"
    assert migration.down_revision == "349_party_role_foundation"
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert '"party_id"' in source
    assert '"party_bound_at"' in source
    assert '"party_binding_source"' in source
    assert '"party_binding_reason"' in source
    assert '"fk_subscribers_party_id"' in source
    assert '"ck_subscribers_party_binding_evidence"' in source
    assert '"ix_subscribers_party_id"' in source
    assert "op.execute" not in source
    assert "op.bulk_insert" not in source


def test_subscriber_party_binding_is_many_accounts_to_one_party():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert "create_unique_constraint" not in source
    assert "unique=True" not in source
