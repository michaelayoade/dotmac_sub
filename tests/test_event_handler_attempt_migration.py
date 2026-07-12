from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "259_reconcile_event_handler_attempts.py"
    )
    spec = importlib.util.spec_from_file_location("migration_259", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _LegacyInspector:
    def get_table_names(self):
        return ["event_handler_attempts"]

    def get_columns(self, _table_name):
        return [
            {"name": "id"},
            {"name": "event_store_id"},
            {"name": "handler_name"},
            {"name": "status"},
            {"name": "error"},
            {"name": "retry_count"},
            {"name": "processed_at"},
            {"name": "created_at"},
        ]

    def get_indexes(self, _table_name):
        return [
            {"name": "ix_event_handler_attempts_event_store_id"},
            {"name": "ix_event_handler_attempts_handler_name"},
        ]


class _RecordingOp:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def add_column(self, table_name, column):
        self.calls.append(("add_column", (table_name, column.name)))

    def execute(self, statement):
        self.calls.append(("execute", str(statement)))

    def alter_column(self, table_name, column_name, **kwargs):
        self.calls.append(("alter_column", (table_name, column_name, kwargs)))

    def drop_column(self, table_name, column_name):
        self.calls.append(("drop_column", (table_name, column_name)))

    def create_index(self, index_name, table_name, columns):
        self.calls.append(("create_index", (index_name, table_name, columns)))


def test_migration_reconciles_legacy_event_attempt_table(monkeypatch):
    migration = _load_migration()
    operations = _RecordingOp()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_inspector", _LegacyInspector)

    migration.upgrade()

    assert (
        "add_column",
        ("event_handler_attempts", "attempted_at"),
    ) in operations.calls
    executed_sql = "\n".join(
        value for operation, value in operations.calls if operation == "execute"
    )
    assert "COALESCE(processed_at, created_at, now())" in executed_sql
    assert (
        "alter_column",
        ("event_handler_attempts", "attempted_at", {"nullable": False}),
    ) in operations.calls
    assert (
        "drop_column",
        ("event_handler_attempts", "processed_at"),
    ) in operations.calls
    assert (
        "drop_column",
        ("event_handler_attempts", "created_at"),
    ) in operations.calls
    assert (
        "create_index",
        (
            "ix_event_handler_attempts_status",
            "event_handler_attempts",
            ["status"],
        ),
    ) in operations.calls
