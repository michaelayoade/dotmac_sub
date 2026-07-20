from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/349_party_role_foundation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_346", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_party_role_foundation_revision_is_linear_and_additive():
    migration = _load_migration()

    assert migration.revision == "349_party_role_foundation"
    assert migration.down_revision == "348_location_capture_prompt_state"
    source = Path(migration.__file__).read_text(encoding="utf-8")

    for table_name in (
        "parties",
        "party_roles",
        "party_relationships",
        "party_memberships",
        "party_contact_points",
        "party_external_references",
    ):
        assert f'"{table_name}"' in source

    assert "resellers" not in source
    assert "vendors" not in source
    assert "subscribers" not in source
    assert "system_users" not in source


def test_party_role_foundation_migration_pins_core_identity_invariants():
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert "ck_parties_merged_target_required" in source
    assert "ck_party_roles_key_contract" in source
    assert "ck_party_relationships_not_self" in source
    assert "ck_party_memberships_not_self" in source
    assert "uq_party_contact_points_primary" in source
    assert "uq_party_external_refs_source_entity_external" in source
