"""Migration contract for the one-periodic-owner access repair cutover."""

from pathlib import Path


def test_access_repair_migration_retires_parallel_schedule_inputs() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/407_retire_parallel_radius_refresh_schedule.py"
    )
    source = path.read_text(encoding="utf-8")

    assert 'down_revision = "406_support_ticket_work_order_provenance"' in source
    assert '"radius_refresh_safety_net_enabled"' in source
    assert '"radius_refresh_safety_net_interval_minutes"' in source
    assert "UPDATE scheduled_tasks SET enabled = false" in source
    assert "Forward-only authority cutover" in source
