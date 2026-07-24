from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/415_permanent_lifecycle_drainage.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_415_permanent_lifecycle_drainage", path
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


def test_migration_pauses_unadmitted_campaigns_and_enables_drainage(monkeypatch):
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    pause = operations.statements[2]
    assert "status = 'paused'" in str(pause)
    assert "campaign_processing_enabled" in str(pause)
    assert "status = 'scheduled'" in str(pause)

    setting_deletes = operations.statements[3:-1]
    deleted = {
        (statement.compile().params["domain"], statement.compile().params["key"])
        for statement in setting_deletes
    }
    assert deleted == set(migration._RETIRED_SETTINGS)

    task_update = operations.statements[-1]
    assert "UPDATE scheduled_tasks SET enabled = true" in str(task_update)
    assert set(task_update.compile().params["task_names"]) == set(
        migration._PERMANENT_TASK_NAMES
    )


def test_migration_is_forward_only(monkeypatch):
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.statements == []
