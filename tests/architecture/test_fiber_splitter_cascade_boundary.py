from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app/services/network/fiber_access_attachments.py"
RESOLVER = PROJECT_ROOT / "app/services/network/fiber_splitter_topology.py"
TRACE_OWNER = PROJECT_ROOT / "app/services/fiber_topology.py"
COMMAND = PROJECT_ROOT / "scripts/network/review_fiber_access_attachments.py"
MODEL = PROJECT_ROOT / "app/models/fiber_access_attachment.py"
NETWORK_MODEL = PROJECT_ROOT / "app/models/network.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/359_splitter_cascade_links.py"


def _constructors(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_access_attachment_owner_is_the_only_cascade_link_constructor():
    writers = {
        str(path.relative_to(PROJECT_ROOT))
        for path in (PROJECT_ROOT / "app").rglob("*.py")
        if "SplitterCascadeLink" in _constructors(path)
    }
    assert writers == {"app/services/network/fiber_access_attachments.py"}


def test_cascade_owner_binds_exact_reviewed_tree_and_loss_evidence():
    source = OWNER.read_text(encoding="utf-8")

    for required in (
        '"splitter_cascade"',
        "preview_access_attachment",
        "proposer cannot review",
        "with_for_update",
        "resolve_splitter_root",
        "splitter_subtree_ids",
        "splitter cascade would create a cycle",
        "explicit insertion_loss_db",
        "created_by_decision_id=decision.id",
        "retired_by_decision_id = decision.id",
        "result_payload",
        "result_sha256",
        'outcome="closed_stale"',
        'outcome="closed_conflict"',
    ):
        assert required in source
    for forbidden in (
        "ST_Distance",
        "splitter_ratio",
        "fdh_id",
        "latitude",
        "longitude",
        "route_geom",
    ):
        assert forbidden not in source


def test_shared_splitter_resolver_is_read_only_and_has_no_inference_fallback():
    source = RESOLVER.read_text(encoding="utf-8")

    assert "resolve_splitter_chain" in source
    assert "resolve_splitter_root" in source
    assert "splitter_subtree_ids" in source
    assert "splitter cascade contains a cycle" in source
    assert "explicit insertion_loss_db" in source
    for forbidden in (
        "db.add(",
        "db.delete(",
        "db.commit(",
        "ST_Distance",
        "splitter_ratio",
        "fdh_id",
        "latitude",
        "longitude",
        "route_geom",
    ):
        assert forbidden not in source


def test_trace_uses_exact_cascade_resolver_and_distribution_edges():
    source = TRACE_OWNER.read_text(encoding="utf-8")

    assert "resolve_splitter_chain(" in source
    assert 'kind="splitter_cascade"' in source
    assert 'segment_kind="distribution_segment"' in source
    assert "cumulative_splitter_loss_db" in source
    for audit_invariant in (
        "splitter_cascade_invalid_ports",
        "splitter_cascade_cycle",
        "splitter_cascade_loss_missing",
        "splitter_cascade_port_role_conflict",
        "splitter_cascade_ambiguous_inputs",
        "splitter_cascade_multiple_upstreams",
        "splitter_cascade_downstream_pon_root",
    ):
        assert audit_invariant in source


def test_model_and_migration_enforce_canonical_cascade_evidence():
    model = MODEL.read_text(encoding="utf-8")
    network_model = NETWORK_MODEL.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")

    assert "class SplitterCascadeLink" in model
    for required in (
        "uq_splitter_cascade_links_active_output",
        "uq_splitter_cascade_links_active_input",
        "ck_splitter_cascade_links_retirement",
        "ck_fiber_access_attachment_cascade_evidence",
        "uq_fiber_access_attachment_active_target",
    ):
        assert required in model
        assert required in migration
    assert "ck_splitters_insertion_loss_db" in network_model
    assert 'revision = "361_splitter_cascade_links"' in migration
    assert 'down_revision = "360_fiber_support_structures"' in migration


def test_operator_command_exposes_cascade_without_a_direct_apply_mode():
    source = COMMAND.read_text(encoding="utf-8")

    assert '"splitter_cascade"' in source
    assert "splitter output-port UUID" in source
    assert "preview_access_attachment(" in source
    assert "propose_access_attachment(" in source
    assert "approve_access_attachment(" in source
    assert "execute_access_attachment(" in source
    assert "SplitterCascadeLink(" not in source
    assert "db.add(" not in source
    assert "db.commit(" not in source
