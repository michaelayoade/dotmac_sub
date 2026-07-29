from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_composable_schema_is_a_forward_expand_migration():
    source = (ROOT / "alembic/versions/437_composable_service_teams.py").read_text(
        encoding="utf-8"
    )

    for table in (
        "service_team_capability_definitions",
        "service_team_capabilities",
        "service_team_responsibility_definitions",
        "service_team_member_responsibilities",
        "service_team_relationships",
        "service_team_scope_bindings",
        "service_team_external_references",
        "outage_team_routing_policies",
    ):
        assert f'"{table}"' in source

    assert 'down_revision = "436_billing_shadow_verification_evidence"' in source
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]
    assert "drop_column" not in upgrade
    assert "drop_table" not in upgrade
    assert "UPDATE service_teams SET team_type" not in upgrade
    assert "INSERT INTO parties" not in source
    assert "INSERT INTO system_users" not in source
    assert "INSERT INTO service_teams" not in source
    assert "INSERT INTO service_team_members" not in source
    assert "team.manager_person_id = member.person_id" not in source
    assert "INSERT INTO service_team_capability_definitions" in source
    assert "INSERT INTO service_team_responsibility_definitions" in source


def test_legacy_manager_pointer_cannot_grant_composed_membership():
    migration = (ROOT / "alembic/versions/437_composable_service_teams.py").read_text(
        encoding="utf-8"
    )
    design = (ROOT / "docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md").read_text(
        encoding="utf-8"
    )

    assert "INSERT INTO service_team_members" not in migration
    assert "manager_person_id = member.person_id" not in migration
    normalized_design = " ".join(design.split())
    assert "never creates membership or operational scope" in normalized_design
    assert (
        "doing so would grant operational scope from a legacy scalar"
        in normalized_design
    )
