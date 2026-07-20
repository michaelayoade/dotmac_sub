from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/382_ticket_work_order_handoff.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "ticket_work_order_migration", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ticket_work_order_handoff_migration_is_irreversible_authority_cutover():
    module = _module()
    source = MIGRATION.read_text()

    assert module.revision == "382_ticket_work_order_handoff"
    assert module.down_revision == "381_operational_sla_policy_events"
    assert "origin_ticket_id" in source
    assert "ForeignKey" not in source  # Alembic uses an explicit named FK operation.
    assert "fk_work_order_origin_ticket_id_support_tickets" in source
    assert "ticket.metadata::jsonb ->> 'work_order_id' = wo.public_id" in source
    assert "crm_ticket_id = NULL" in source
    assert "metadata::jsonb - 'work_order_id'" in source

    with pytest.raises(RuntimeError, match="authority cutover is irreversible"):
        module.downgrade()
