from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/414_permanent_device_projection.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_414_permanent_device_projection", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOperations:
    def __init__(self) -> None:
        self.statements = []

    def execute(self, statement) -> None:
        self.statements.append(statement)


def test_migration_retires_the_control_and_enables_projection_repair(monkeypatch):
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    setting_delete, task_update = operations.statements
    assert "DELETE FROM domain_settings" in str(setting_delete)
    assert setting_delete.compile().params == {
        "domain": "network_monitoring",
        "key": "device_projection_reconcile_enabled",
    }

    assert "UPDATE scheduled_tasks SET enabled = true" in str(task_update)
    assert task_update.compile().params == {
        "name": "device_projection_reconcile",
        "task_name": "app.tasks.device_projection.reconcile_device_projections",
    }


def test_migration_is_forward_only(monkeypatch):
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.statements == []
