from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "alembic/versions/510_inbox_manager_ai_permission.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_510", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inbox_ai_permission_follows_operator_tenant_migrations() -> None:
    migration = _load_migration()

    assert migration.revision == "510_inbox_manager_ai_permission"
    assert migration.down_revision == "509_backfill_operator_tenant_scope"
