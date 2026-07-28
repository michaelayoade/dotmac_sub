from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/398_permanent_customer_financial_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_398_permanent_financial_lifecycle", path
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


def test_migration_removes_every_retired_row_and_enables_permanent_tasks(monkeypatch):
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    setting_deletes = operations.statements[: len(migration._RETIRED_SETTINGS)]
    deleted = {
        (statement.compile().params["domain"], statement.compile().params["key"])
        for statement in setting_deletes
    }
    assert deleted == set(migration._RETIRED_SETTINGS)

    event_delete = operations.statements[-2]
    assert "notification_event_%_enabled" in str(event_delete)

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
