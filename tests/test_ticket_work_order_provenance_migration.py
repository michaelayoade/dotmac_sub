from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/406_support_ticket_work_order_provenance.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "support_ticket_work_order_provenance", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provenance_backfill_is_exact_verified_and_preserving():
    module = _module()
    source = MIGRATION.read_text(encoding="utf-8")

    assert module.revision == "406_support_ticket_work_order_provenance"
    assert module.down_revision == "405_restore_wireless_masts"
    assert "metadata::jsonb ->> 'crm_ticket_id'" in source
    assert "wo.subscriber_id IS DISTINCT FROM ticket.subscriber_id" in source
    assert "wo.origin_ticket_id IS DISTINCT FROM ticket.id" in source
    assert "SET origin_ticket_id = ticket.id" in source
    assert "SET crm_ticket_id" not in source
    assert "wo.title" not in source

    with pytest.raises(RuntimeError, match="cutover is irreversible"):
        module.downgrade()
