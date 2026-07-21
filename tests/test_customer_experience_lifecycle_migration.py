from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/386_customer_experience_lifecycle_sot.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "customer_lifecycle_migration", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_customer_lifecycle_migration_is_fail_closed_irreversible_cutover():
    module = _module()
    source = MIGRATION.read_text()

    assert module.revision == "386_customer_experience_lifecycle_sot"
    assert module.down_revision == "385_rbac_catalog_normalized_identity"
    assert "project_task_id" in source
    assert "fk_work_order_project_task_id_project_tasks" in source
    assert "unresolved imported links" in source
    assert "conflicting project links" in source
    assert 'op.drop_column("project_tasks", "work_order_id")' in source
    assert 'op.drop_table("project_mirror")' in source
    assert 'op.drop_table("work_order_sync_state")' in source
    assert "crm_work_order_pull" in source
    assert "projects_native_read" in source

    with pytest.raises(RuntimeError, match="cutover is irreversible"):
        module.downgrade()
