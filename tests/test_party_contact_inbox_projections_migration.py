from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/354_party_contact_inbox_projections.py"
    )
    spec = importlib.util.spec_from_file_location("migration_351", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contact_inbox_revision_is_linear_and_schema_only():
    migration = _load_migration()

    assert migration.revision == "354_party_contact_inbox_projections"
    assert migration.down_revision == "353_party_principal_context_bindings"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    for table_name in (
        "subscriber_contacts",
        "subscriber_contact_relationship_projections",
        "subscriber_contact_point_projections",
        "inbox_contact_links",
    ):
        assert f'"{table_name}"' in source
    for column_name in (
        "person_party_id",
        "party_relationship_id",
        "party_contact_point_id",
        "party_contact_point_binding_source",
    ):
        assert f'"{column_name}"' in source
    assert "create_foreign_key" in source
    assert "create_check_constraint" in source
    assert "op.execute" not in source
    assert "op.bulk_insert" not in source


def test_migration_does_not_copy_contact_or_routing_state():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "verification_status =",
        "consent_status =",
        "is_authorized =",
        "is_billing_contact =",
        "receives_notifications =",
        "subscriber_id =",
        "reseller_id =",
        "normalized_contact =",
    ):
        assert forbidden not in source
