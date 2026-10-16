"""Migration contract for append-only service-extension reversals."""

from __future__ import annotations

from pathlib import Path

from tests.migration_graph_helpers import composed_sub_graph

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/472_service_extension_reversals.py"


def test_service_extension_reversal_is_the_single_additive_head() -> None:
    script, heads = composed_sub_graph(ROOT)

    # Single-headed with this revision in the head's ancestry. Naming the exact
    # head made every later migration break this test for no defect.
    assert len(heads) == 1
    assert "472_service_extension_reversals" in {
        item.revision
        for item in script.iterate_revisions(
            heads[0], "472_service_extension_reversals", inclusive=True
        )
    }
    assert (
        script.get_revision("472_service_extension_reversals").down_revision
        == "471_quote_documents_and_delivery"
    )


def test_service_extension_reversal_migration_preserves_original_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert (
        "ALTER TYPE serviceextensionstatus ADD VALUE IF NOT EXISTS 'reversed'" in source
    )
    assert '"service_extension_reversals"' in source
    assert '"service_extension_reversal_entries"' in source
    assert "uq_service_extension_reversals_extension" in source
    assert "uq_service_extension_reversal_entries_extension_entry" in source
    assert 'ondelete="RESTRICT"' in source
    assert '"billing:extension:reverse"' in source
    assert "role_permissions.insert()" in source
    assert "DELETE FROM service_extension_entries" not in source
    assert "UPDATE service_extension_entries" not in source
