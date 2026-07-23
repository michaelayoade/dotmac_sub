from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/408_radius_session_latest_projection.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "radius_session_latest_projection", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_radius_session_projection_advances_the_single_migration_head() -> None:
    module = _module()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert module.revision == "408_radius_session_latest_projection"
    assert module.down_revision == "407_retire_parallel_radius_refresh"
    assert script.get_heads() == ["408_radius_session_latest_projection"]


def test_radius_session_projection_uses_nonblocking_postgres_ddl() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "autocommit_block()" in source
    assert "COALESCE(last_update_at, session_start, created_at)" in source
