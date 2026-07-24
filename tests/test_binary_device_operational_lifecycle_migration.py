"""Contract tests for migration 416."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/416_binary_device_operational_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_416_binary_device_operational_lifecycle", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binary_device_operational_lifecycle_migration_contract() -> None:
    migration = _load_migration()

    assert migration.revision == "416_binary_device_operational_lifecycle"
    assert migration.down_revision == "415_permanent_lifecycle_drainage"
    assert migration._RETIRED_SETTINGS == (
        ("network_monitoring", "monitoring_coverage_enabled"),
        ("network_monitoring", "monitoring_inventory_sync_enabled"),
        ("network_monitoring", "channel_health_enabled"),
    )
    assert set(migration._PERMANENT_TASK_NAMES) == {
        "app.tasks.monitoring_coverage.refresh_monitoring_coverage",
        "app.tasks.monitoring_cleanup.sync_inventory_to_monitoring",
        "app.tasks.channel_health.observe_channel_health",
    }


def test_binary_device_operational_lifecycle_is_forward_only() -> None:
    migration = _load_migration()

    assert migration.downgrade() is None
