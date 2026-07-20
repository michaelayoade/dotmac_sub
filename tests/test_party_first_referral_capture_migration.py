from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/356_party_first_referral_capture.py"
    )
    spec = importlib.util.spec_from_file_location("migration_353", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_party_first_referral_revision_is_linear_and_additive():
    migration = _load_migration()

    assert migration.revision == "356_party_first_referral_capture"
    assert migration.down_revision == "355_party_customer_lifecycle"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    for column_name in (
        "referred_party_id",
        "party_binding_source",
        "subscriber_linked_at",
        "subscriber_link_reason",
    ):
        assert f'"{column_name}"' in source
    assert "uq_referrals_active_referred_party" in source
    assert "create_foreign_key" in source
    assert "create_check_constraint" in source
    assert "nullable=True" in source


def test_migration_does_not_infer_identity_or_change_lifecycle_state():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "UPDATE referrals",
        "UPDATE subscribers",
        "INSERT INTO parties",
        "INSERT INTO leads",
        "INSERT INTO party_contact_points",
        "status = 'qualified'",
        "status = 'blocked'",
        "op.bulk_insert",
    ):
        assert forbidden not in source
