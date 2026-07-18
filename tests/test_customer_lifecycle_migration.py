from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/355_party_customer_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("migration_352", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_customer_lifecycle_revision_is_linear_and_additive():
    migration = _load_migration()

    assert migration.revision == "355_party_customer_lifecycle"
    assert migration.down_revision == "354_party_contact_inbox_projections"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    for table_name in (
        "leads",
        "lead_origin_captures",
        "campaigns",
        "campaign_recipients",
        "support_tickets",
    ):
        assert table_name in source
    for column_name in (
        "party_id",
        "subscriber_linked_at",
        "capture_method",
        "source_platform",
        "external_campaign_id",
    ):
        assert column_name in source
    assert "NOT VALID" in source
    assert "nullable=True" in source
    assert "op.bulk_insert" not in source


def test_migration_does_not_infer_or_change_business_lifecycle_state():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "UPDATE leads",
        "UPDATE subscribers",
        "UPDATE subscriptions",
        "INSERT INTO leads",
        "INSERT INTO parties",
        "status = 'blocked'",
        "status = 'active'",
    ):
        assert forbidden not in source
