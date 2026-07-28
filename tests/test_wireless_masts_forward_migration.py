from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/405_restore_wireless_masts.py"


def test_wireless_masts_forward_repair_follows_current_head() -> None:
    source = MIGRATION.read_text()

    assert 'revision = "405_restore_wireless_masts"' in source
    assert 'down_revision = "404_team_inbox_sot_completion"' in source
    assert '"wireless_masts"' in source
    assert '"fk_wireless_masts_pop_site_id"' in source
    assert '"idx_wireless_masts_geom"' in source
    assert 'name="wirelessmaststatus"' in source
    assert "checkfirst=True" in source


def test_wireless_masts_forward_repair_does_not_drop_inventory() -> None:
    source = MIGRATION.read_text()

    downgrade = source.split("def downgrade() -> None:", 1)[1]
    assert "drop_table" not in downgrade
