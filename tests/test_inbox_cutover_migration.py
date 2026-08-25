"""Inbox cutover support tables stay additive and on Sub's single lineage."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "alembic/versions/550_inbox_queue_bindings.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "inbox_queue_bindings_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inbox_cutover_support_tables_extend_the_single_sub_lineage() -> None:
    migration = _load_migration()
    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    heads = script.get_heads()

    assert migration.revision == "550_inbox_queue_bindings"
    assert migration.down_revision == "549_gateway_intent_lifecycle"
    assert len(heads) == 1
    assert any(
        revision.revision == migration.revision
        for revision in script.iterate_revisions(
            heads[0], migration.revision, inclusive=True
        )
    )


def test_inbox_cutover_migration_is_schema_only_and_keeps_roster_reason() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    for table_name in (
        "inbox_queue_bindings",
        "inbox_agent_presence_details",
    ):
        assert f'"{table_name}"' in source
    for contract in (
        "uq_inbox_queue_bindings_service_team",
        "uq_inbox_queue_bindings_queue",
        "ck_inbox_agent_presence_details_away_reason",
        "'away', 'break'",
    ):
        assert contract in source
    assert "op.execute" not in source
    assert "op.bulk_insert" not in source
