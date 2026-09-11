"""Contract tests for the active CPE/TR-069 identity migration."""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic/versions/595_active_cpe_identity.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_595_active_cpe_identity", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_refuses_duplicate_active_cpe_links(monkeypatch) -> None:
    migration = _migration()
    cpe_id = uuid.uuid4()
    result = SimpleNamespace(fetchall=lambda: [(cpe_id, 2)])
    bind = SimpleNamespace(execute=lambda _statement: result)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    with pytest.raises(RuntimeError, match="duplicate active links"):
        migration.upgrade()


def test_migration_declares_matching_partial_unique_index() -> None:
    source = Path("alembic/versions/595_active_cpe_identity.py").read_text(
        encoding="utf-8"
    )

    assert "uq_tr069_cpe_devices_active_cpe_device_id" in source
    assert (
        'postgresql_where=sa.text("is_active AND cpe_device_id IS NOT NULL")' in source
    )
    assert 'sqlite_where=sa.text("is_active AND cpe_device_id IS NOT NULL")' in source
    assert "GROUP BY cpe_device_id HAVING count(*) > 1" in source
