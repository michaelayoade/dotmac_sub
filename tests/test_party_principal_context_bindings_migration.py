from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/353_party_principal_context_bindings.py"
    )
    spec = importlib.util.spec_from_file_location("migration_350", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_principal_context_revision_is_linear_and_schema_only():
    migration = _load_migration()

    assert migration.revision == "353_party_principal_context_bindings"
    assert migration.down_revision == "352_party_organization_profile_bindings"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    for table_name in (
        "system_users",
        "reseller_users",
        "organization_memberships",
        "field_vendor_users",
    ):
        assert f'"{table_name}"' in source
    for column_name in (
        "person_party_id",
        "party_membership_id",
        "party_bound_at",
        "party_binding_source",
        "party_binding_reason",
    ):
        assert f'"{column_name}"' in source
    assert "create_foreign_key" in source
    assert "create_unique_constraint" in source
    assert "party_binding_evidence" in source
    assert '"vendor_users"' not in source
    assert "op.execute" not in source
    assert "op.bulk_insert" not in source


def test_migration_does_not_change_auth_or_compatibility_state():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "user_credentials",
        "system_user_roles",
        "system_user_permissions",
        "is_active =",
        "membership_type =",
        "account_type",
    ):
        assert forbidden not in source
